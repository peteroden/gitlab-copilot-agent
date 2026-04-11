"""ReviewPipeline — structured four-stage MR review.

Extracts the monolithic ``handle_review()`` into the Pipeline protocol:
prepare (clone + fetch) → execute (LLM review) → process (post comments)
→ cleanup (remove repo + record metrics).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import TYPE_CHECKING

import structlog
from pydantic import Field

from gitlab_copilot_agent.comment_parser import ParsedReview, parse_review
from gitlab_copilot_agent.comment_poster import post_review
from gitlab_copilot_agent.discussion_models import DiscussionHistory
from gitlab_copilot_agent.git import validate_clone_url_host
from gitlab_copilot_agent.gitlab_client import (
    GitLabClient,  # noqa: TC001 — used in constructor + method bodies
    MRChange,  # noqa: TC001 — used in format_diff_text
    MRDetails,  # noqa: TC001 — Pydantic runtime field type
)
from gitlab_copilot_agent.incremental import extract_last_reviewed_sha
from gitlab_copilot_agent.metrics import reviews_duration, reviews_total
from gitlab_copilot_agent.pipeline import BasePipelineContext, post_pipeline_error, stage_requires
from gitlab_copilot_agent.review_engine import ReviewRequest, run_review
from gitlab_copilot_agent.task_executor import (
    TaskResult,  # noqa: TC001 — Pydantic runtime field type
)

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.credential_registry import CredentialRegistry
    from gitlab_copilot_agent.events import TaskEvent
    from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()


def format_diff_text(changes: list[MRChange]) -> str:
    """Format MR changes into a unified diff string."""
    return "\n".join(f"--- a/{c.old_path}\n+++ b/{c.new_path}\n{c.diff}" for c in changes)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class ReviewContext(BasePipelineContext):
    """Mutable state threaded through ReviewPipeline stages."""

    # Set by prepare
    diff_text: str = ""
    is_incremental: bool = False
    discussion_history: DiscussionHistory | None = None
    commit_messages: list[str] = Field(default_factory=list)
    mr_details: MRDetails | None = None

    # Set by execute
    raw_result: TaskResult | None = None

    # Set by process
    parsed: ParsedReview | None = None
    comments_posted: int = 0
    resolutions_posted: int = 0

    # Timing
    start_time: float = 0.0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class ReviewPipeline:
    """Four-stage review pipeline.

    Replaces the monolithic ``handle_review()`` function:

    - **prepare**: clone, fetch MR details + discussions, build diff
    - **execute**: build prompt, run Copilot review session
    - **process**: parse review, post comments/resolutions/summary
    - **cleanup**: remove cloned repo, record metrics
    """

    def __init__(
        self,
        settings: Settings,
        event: TaskEvent,
        executor: TaskExecutor,
        gl_client: GitLabClient,
        credential_registry: CredentialRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._event = event
        self._executor = executor
        self._gl = gl_client
        self._creds = credential_registry
        self._log = log.bind(
            project_id=event.project_id,
            mr_iid=event.mr_iid,
        )

    # -- prepare -----------------------------------------------------------

    async def prepare(self, pipeline_context: ReviewContext) -> None:
        """Clone repo, fetch MR details + discussion history, build diff."""
        event = self._event
        settings = self._settings
        # Validated non-None by TaskEvent model_validator for review events
        mr_iid: int = event.mr_iid  # type: ignore[assignment]
        head_sha: str = event.head_sha  # type: ignore[assignment]
        pipeline_context.start_time = time.monotonic()

        await self._log.ainfo("review_started")

        validate_clone_url_host(event.clone_url, settings.gitlab_url)
        pipeline_context.repo_path = await self._gl.clone_repo(
            event.clone_url,
            event.branch,
            event.token,
            clone_dir=settings.clone_dir,
        )

        pipeline_context.mr_details = await self._gl.get_mr_details(event.project_id, mr_iid)

        # Fetch commit messages (graceful degradation)
        try:
            commits = await self._gl.get_mr_commits(event.project_id, mr_iid)
            pipeline_context.commit_messages = [c.message for c in commits]
            await self._log.ainfo("commits_loaded", commit_count=len(commits))
        except Exception:
            await self._log.awarning("commit_fetch_failed", exc_info=True)

        # Fetch discussion history (requires credential_registry)
        if self._creds is not None:
            try:
                discussions = await self._gl.list_mr_discussions(event.project_id, mr_iid)
                agent_identity = await self._creds.resolve_identity(
                    event.credential_ref, settings.gitlab_url
                )
                pipeline_context.discussion_history = DiscussionHistory(
                    discussions=discussions, agent=agent_identity
                )
                await self._log.ainfo(
                    "discussion_history_loaded",
                    discussion_count=len(discussions),
                    agent_user_id=agent_identity.user_id,
                )
            except Exception:
                await self._log.awarning("discussion_history_failed", exc_info=True)
        else:
            await self._log.adebug("discussion_history_skipped", reason="no_credential_registry")

        # Build diff text — incremental when a prior review marker exists
        last_reviewed_sha = extract_last_reviewed_sha(pipeline_context.discussion_history)

        if last_reviewed_sha and last_reviewed_sha != head_sha:
            try:
                incremental_changes = await self._gl.compare_commits(
                    event.project_id, last_reviewed_sha, head_sha
                )
                if incremental_changes:
                    pipeline_context.diff_text = format_diff_text(incremental_changes)
                    pipeline_context.is_incremental = True
                    await self._log.ainfo(
                        "incremental_review",
                        from_sha=last_reviewed_sha[:12],
                        to_sha=head_sha[:12],
                        files_changed=len(incremental_changes),
                    )
            except Exception:
                await self._log.awarning("incremental_diff_failed", exc_info=True)

        if not pipeline_context.is_incremental:
            mr_details = stage_requires(pipeline_context.mr_details, "mr_details")
            pipeline_context.diff_text = format_diff_text(mr_details.changes)

    # -- execute -----------------------------------------------------------

    async def execute(self, pipeline_context: ReviewContext) -> None:
        """Build review prompt and run Copilot session."""
        event = self._event
        mr_details = stage_requires(pipeline_context.mr_details, "mr_details")
        head_sha = event.head_sha or ""

        review_req = ReviewRequest(
            title=mr_details.title,
            description=mr_details.description,
            source_branch=event.branch,
            target_branch=event.target_branch,
            commit_messages=pipeline_context.commit_messages,
        )

        raw_result = await run_review(
            self._executor,
            self._settings,
            str(pipeline_context.repo_path),
            event.clone_url,
            review_req,
            diff_text=pipeline_context.diff_text,
            discussion_history=pipeline_context.discussion_history,
            head_sha=head_sha,
            is_incremental=pipeline_context.is_incremental,
        )
        pipeline_context.raw_result = raw_result

        parsed = parse_review(raw_result.summary)
        pipeline_context.parsed = parsed

        await self._log.ainfo(
            "review_complete",
            inline_comments=len(parsed.comments),
            resolutions=len(parsed.resolutions),
            response_length=len(raw_result.summary),
        )

    # -- process -----------------------------------------------------------

    async def process(self, pipeline_context: ReviewContext) -> None:
        """Post review comments, resolutions, and summary to GitLab."""
        event = self._event
        mr_details = stage_requires(pipeline_context.mr_details, "mr_details")
        parsed = stage_requires(pipeline_context.parsed, "parsed")

        # Build allowlist of discussion IDs from agent's prior unresolved feedback
        allowed_ids: set[str] = set()
        if pipeline_context.discussion_history:
            for disc in pipeline_context.discussion_history.discussions:
                if disc.is_resolved or not disc.is_inline:
                    continue
                if not disc.notes:
                    continue
                if disc.notes[0].author_id == pipeline_context.discussion_history.agent.user_id:
                    allowed_ids.add(disc.discussion_id)

        await post_review(
            self._gl,
            event.project_id,
            event.mr_iid,  # type: ignore[arg-type]  # validated non-None by TaskEvent
            mr_details.diff_refs,
            parsed,
            mr_details.changes,
            resolution_behavior=event.resolution_behavior,
            allowed_discussion_ids=frozenset(allowed_ids),
            head_sha=event.head_sha or "",
        )
        await self._log.ainfo("comments_posted")

    # -- cleanup -----------------------------------------------------------

    async def cleanup(self, pipeline_context: ReviewContext) -> None:
        """Remove cloned repo and record metrics."""
        if pipeline_context.repo_path:
            await asyncio.to_thread(shutil.rmtree, pipeline_context.repo_path, True)
        reviews_total.add(1, {"outcome": pipeline_context.outcome})
        reviews_duration.record(
            time.monotonic() - pipeline_context.start_time, {"outcome": pipeline_context.outcome}
        )

    # -- error handling ----------------------------------------------------

    async def handle_error(self, pipeline_context: ReviewContext, exc: Exception) -> None:
        """Post failure comment to MR."""
        event = self._event
        if event.mr_iid is None:
            return

        async def _post(msg: str) -> None:
            await self._gl.post_mr_comment(event.project_id, event.mr_iid, msg)  # type: ignore[arg-type]

        await post_pipeline_error(self._log, exc, _post)
