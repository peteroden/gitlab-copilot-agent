"""DiscussionPipeline — structured four-stage thread interaction handler.

Extracts ``handle_discussion_interaction()`` into the Pipeline protocol:
prepare (clone + fetch context) → execute (LLM discussion) →
process (apply patch + post reply + resolve) → cleanup (remove repo).
"""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.coding_workflow import apply_coding_result
from gitlab_copilot_agent.discussion_engine import (
    DiscussionResponse,  # noqa: TC001 — Pydantic runtime field type
    build_discussion_prompt,
    parse_discussion_response,
    run_discussion,
)
from gitlab_copilot_agent.discussion_models import (
    AgentIdentity,  # noqa: TC001 — runtime usage in constructor
    Discussion,
    DiscussionHistory,
)
from gitlab_copilot_agent.error_messages import branch_deleted_message, user_error_message
from gitlab_copilot_agent.git_operations import (
    TransientCloneError,
    git_commit,
    git_push,
    validate_clone_url_host,
)
from gitlab_copilot_agent.gitlab_client import GitLabClient, MRDetails  # noqa: TC001
from gitlab_copilot_agent.pipeline import BasePipelineContext, post_pipeline_error, stage_requires
from gitlab_copilot_agent.prompt_defaults import get_prompt
from gitlab_copilot_agent.task_executor import (
    CodingResult,
    TaskExecutionError,
    TaskResult,  # noqa: TC001 — Pydantic runtime field type
)

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.events import TaskEvent
    from gitlab_copilot_agent.mapping_models import ResolutionBehavior
    from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class DiscussionContext(BasePipelineContext):
    """Mutable state threaded through DiscussionPipeline stages."""

    # Set by prepare
    mr_details: MRDetails | None = None
    discussions: list[Discussion] | None = None
    discussion_history: DiscussionHistory | None = None
    triggering: Discussion | None = None
    branch_deleted: bool = False

    # Set by execute
    raw_result: TaskResult | None = None
    response: DiscussionResponse | None = None

    # Set by process
    reply_posted: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_triggering_discussion(
    discussions: list[Discussion],
    note_id: int,
) -> Discussion | None:
    """Find the discussion containing the note that triggered this handler."""
    for disc in discussions:
        for note in disc.notes:
            if note.note_id == note_id:
                return disc
    return None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class DiscussionPipeline:
    """Four-stage discussion pipeline.

    Replaces the monolithic ``handle_discussion_interaction()`` function:

    - **prepare**: clone, fetch MR + discussions, find triggering thread
    - **execute**: build prompt, run Copilot discussion session
    - **process**: apply patch, post reply, handle thread resolution
    - **cleanup**: remove cloned repo
    """

    def __init__(
        self,
        settings: Settings,
        event: TaskEvent,
        executor: TaskExecutor,
        gl_client: GitLabClient,
        agent_identity: AgentIdentity,
    ) -> None:
        self._settings = settings
        self._event = event
        self._executor = executor
        self._gl = gl_client
        self._agent = agent_identity
        self._log = log.bind(
            project_id=event.project_id,
            mr_iid=event.mr_iid,
            note_id=event.note_id,
        )

    # -- prepare -----------------------------------------------------------

    async def prepare(self, pipeline_context: DiscussionContext) -> None:
        """Clone repo, fetch MR + discussions, find triggering thread."""
        event = self._event
        settings = self._settings
        # Validated non-None by TaskEvent model_validator for discussion events
        mr_iid: int = event.mr_iid  # type: ignore[assignment]
        note_id: int = event.note_id  # type: ignore[assignment]

        await self._log.ainfo("discussion_interaction_started")

        # Clone repo (always — questions may need full context)
        try:
            validate_clone_url_host(event.clone_url, settings.gitlab_url)
            pipeline_context.repo_path = await self._gl.clone_repo(
                event.clone_url,
                event.branch,
                event.token,
                clone_dir=settings.clone_dir,
            )
        except (RuntimeError, TransientCloneError) as clone_exc:
            clone_err = str(clone_exc).lower()
            if "not found" in clone_err or "not allowed" in clone_err:
                await self._log.awarning(
                    "branch_deleted_or_inaccessible",
                    branch=event.branch,
                    error=str(clone_exc),
                )
                await self._reply_branch_deleted(mr_iid, note_id)
                pipeline_context.branch_deleted = True
                pipeline_context.outcome = "branch_deleted"
                return
            raise

        # Fetch MR details + discussions
        pipeline_context.mr_details = await self._gl.get_mr_details(event.project_id, mr_iid)
        pipeline_context.discussions = await self._gl.list_mr_discussions(event.project_id, mr_iid)
        pipeline_context.discussion_history = DiscussionHistory(
            discussions=pipeline_context.discussions, agent=self._agent
        )

        # Find the triggering discussion thread
        pipeline_context.triggering = _find_triggering_discussion(
            pipeline_context.discussions, note_id
        )
        if pipeline_context.triggering is None:
            await self._log.awarning("triggering_discussion_not_found", note_id=note_id)
            pipeline_context.outcome = "not_found"

    async def _reply_branch_deleted(self, mr_iid: int, note_id: int) -> None:
        """Try to reply in the triggering thread about a deleted branch."""
        event = self._event
        try:
            discussions = await self._gl.list_mr_discussions(event.project_id, mr_iid)
            triggering = _find_triggering_discussion(discussions, note_id)
            if triggering:
                await self._gl.reply_to_discussion(
                    event.project_id,
                    mr_iid,
                    triggering.discussion_id,
                    branch_deleted_message(event.branch),
                )
        except Exception:
            await self._log.awarning("branch_deleted_reply_failed", exc_info=True)

    # -- execute -----------------------------------------------------------

    async def execute(self, pipeline_context: DiscussionContext) -> None:
        """Build discussion prompt and run Copilot session."""
        if pipeline_context.branch_deleted or pipeline_context.triggering is None:
            return

        mr_details = stage_requires(pipeline_context.mr_details, "mr_details")
        discussion_history = stage_requires(
            pipeline_context.discussion_history, "discussion_history"
        )

        pipeline_context.raw_result = await run_discussion(
            self._executor,
            self._settings,
            str(pipeline_context.repo_path),
            self._event.clone_url,
            system_prompt=get_prompt(self._settings, "discussion"),
            user_prompt=build_discussion_prompt(
                mr_details,
                discussion_history,
                pipeline_context.triggering,
            ),
            source_branch=self._event.branch,
            note_id=self._event.note_id,  # type: ignore[arg-type]
        )

        pipeline_context.response = parse_discussion_response(pipeline_context.raw_result.summary)

        has_patch = isinstance(pipeline_context.raw_result, CodingResult) and bool(
            pipeline_context.raw_result.patch
        )
        await self._log.ainfo(
            "discussion_response_parsed",
            has_code_changes=has_patch,
        )

    # -- process -----------------------------------------------------------

    async def process(self, pipeline_context: DiscussionContext) -> None:
        """Apply patch, post reply, handle thread resolution."""
        if pipeline_context.branch_deleted or pipeline_context.triggering is None:
            return

        raw_result = stage_requires(pipeline_context.raw_result, "raw_result")
        response = stage_requires(pipeline_context.response, "response")
        repo_path = stage_requires(pipeline_context.repo_path, "repo_path")

        event = self._event
        # Validated non-None by TaskEvent model_validator for discussion events
        mr_iid: int = event.mr_iid  # type: ignore[assignment]

        # Apply code changes if present
        has_patch = isinstance(raw_result, CodingResult) and bool(raw_result.patch)
        commit_subject = (event.note_body or "discussion fix")[:50]

        if has_patch:
            await apply_coding_result(raw_result, repo_path)
            has_changes = await git_commit(
                repo_path,
                f"fix: {commit_subject}",
                self._settings.agent_author_name,
                self._settings.agent_author_email,
            )
            if has_changes:
                await git_push(repo_path, "origin", event.branch, event.token)
                response = response.model_copy(
                    update={"reply": f"{response.reply}\n\n✅ Changes pushed."}
                )

        # Post reply to the existing thread
        await self._gl.reply_to_discussion(
            event.project_id,
            mr_iid,
            pipeline_context.triggering.discussion_id,
            response.reply,
        )
        await self._log.ainfo("discussion_reply_posted")
        pipeline_context.reply_posted = True

        # Handle resolution
        await self._handle_resolution(pipeline_context, response)

    async def _handle_resolution(
        self,
        pipeline_context: DiscussionContext,
        response: DiscussionResponse,
    ) -> None:
        """Resolve thread if appropriate based on resolution behavior."""
        triggering = stage_requires(pipeline_context.triggering, "triggering")
        event = self._event
        # Validated non-None by TaskEvent model_validator for discussion events
        mr_iid: int = event.mr_iid  # type: ignore[assignment]
        resolution_behavior: ResolutionBehavior = event.resolution_behavior

        first_note = triggering.notes[0] if triggering.notes else None
        is_agent_thread = (
            first_note is not None
            and triggering.is_inline
            and first_note.author_id == self._agent.user_id
        )

        if response.resolution and resolution_behavior != "off" and is_agent_thread:
            try:
                if (
                    response.resolution.status == "resolved"
                    and resolution_behavior == "auto-resolve"
                ):
                    await self._gl.resolve_discussion(
                        event.project_id,
                        mr_iid,
                        triggering.discussion_id,
                    )
                    await self._log.ainfo(
                        "discussion_auto_resolved",
                        discussion_id=triggering.discussion_id,
                    )
                elif response.resolution.status == "partial":
                    await self._log.ainfo(
                        "discussion_partial_resolution",
                        discussion_id=triggering.discussion_id,
                    )
            except Exception:
                await self._log.awarning(
                    "discussion_resolution_failed",
                    discussion_id=triggering.discussion_id,
                    exc_info=True,
                )

    # -- cleanup -----------------------------------------------------------

    async def cleanup(self, pipeline_context: DiscussionContext) -> None:
        """Remove cloned repo."""
        if pipeline_context.repo_path:
            await asyncio.to_thread(shutil.rmtree, pipeline_context.repo_path, True)

    # -- error handling ----------------------------------------------------

    async def handle_error(self, pipeline_context: DiscussionContext, exc: Exception) -> None:
        """Post failure comment to MR."""
        event = self._event
        if event.mr_iid is None:
            return

        async def _post(msg: str) -> None:
            await self._gl.post_mr_comment(event.project_id, event.mr_iid, msg)  # type: ignore[arg-type]

        if isinstance(exc, TaskExecutionError):
            # Post sanitized message directly — no prefix wrapper
            await self._log.aerror("pipeline_task_failed", error=str(exc))
            try:
                await _post(user_error_message(str(exc)))
            except Exception:
                await self._log.awarning("failure_comment_post_failed", exc_info=True)
            return

        await post_pipeline_error(
            self._log,
            exc,
            _post,
            generic_msg=(
                "❌ Unable to process your request. "
                "The service encountered an unexpected error. "
                "Please try again or contact the project administrator."
            ),
        )
