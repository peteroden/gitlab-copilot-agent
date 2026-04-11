"""CodingPipeline — structured four-stage Jira-to-MR coding workflow.

Extracts ``CodingOrchestrator.handle()`` into the Pipeline protocol:
prepare (clone + branch) → execute (LLM coding) →
process (apply + commit + push + MR + Jira) → cleanup (remove repo + metrics).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.coding_engine import run_coding_task, strip_json_block
from gitlab_copilot_agent.coding_workflow import apply_coding_result
from gitlab_copilot_agent.error_messages import user_error_message
from gitlab_copilot_agent.git import (
    TransientCloneError,
    git_clone,
    git_commit,
    git_push,
    git_unique_branch,
)
from gitlab_copilot_agent.gitlab_client import GitLabClient  # noqa: TC001
from gitlab_copilot_agent.jira_client import JiraClient  # noqa: TC001
from gitlab_copilot_agent.jira_models import JiraIssue  # noqa: TC001
from gitlab_copilot_agent.metrics import coding_tasks_duration, coding_tasks_total
from gitlab_copilot_agent.pipeline import BasePipelineContext, post_pipeline_error, stage_requires
from gitlab_copilot_agent.project_registry import ResolvedProject  # noqa: TC001
from gitlab_copilot_agent.task_executor import (
    TaskExecutionError,
    TaskResult,  # noqa: TC001 — Pydantic runtime field type
)

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class CodingContext(BasePipelineContext):
    """Mutable state threaded through CodingPipeline stages."""

    # Set by prepare
    branch: str = ""
    raw_result: TaskResult | None = None

    # Timing
    start_time: float = 0.0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class CodingPipeline:
    """Four-stage coding pipeline.

    Replaces the monolithic ``CodingOrchestrator.handle()`` function:

    - **prepare**: clone repo, create unique branch, transition Jira to In Progress
    - **execute**: run Copilot coding session
    - **process**: apply patch, commit, push, create MR, update Jira
    - **cleanup**: remove cloned repo, record metrics
    """

    def __init__(
        self,
        settings: Settings,
        issue: JiraIssue,
        project_mapping: ResolvedProject,
        executor: TaskExecutor,
        gitlab_client: GitLabClient,
        jira_client: JiraClient,
    ) -> None:
        self._settings = settings
        self._issue = issue
        self._mapping = project_mapping
        self._executor = executor
        self._gl = gitlab_client
        self._jira = jira_client
        self._log = log.bind(
            issue_key=issue.key,
            project_id=project_mapping.gitlab_project_id,
        )

    # -- prepare -----------------------------------------------------------

    async def prepare(self, pipeline_context: CodingContext) -> None:
        """Clone repo, create unique branch, transition Jira to In Progress."""
        pipeline_context.start_time = time.monotonic()
        await self._log.ainfo("coding_task_started")

        in_prog = self._mapping.in_progress_status
        await self._jira.transition_issue(self._issue.key, in_prog)

        pipeline_context.repo_path = await git_clone(
            self._mapping.clone_url,
            self._mapping.target_branch,
            self._mapping.token,
            clone_dir=self._settings.clone_dir,
            max_retries=self._settings.git_clone_max_retries,
            backoff_base=self._settings.git_clone_backoff_base,
        )
        pipeline_context.branch = await git_unique_branch(
            pipeline_context.repo_path, f"agent/{self._issue.key.lower()}"
        )

    # -- execute -----------------------------------------------------------

    async def execute(self, pipeline_context: CodingContext) -> None:
        """Run Copilot coding session."""
        description = (
            self._issue.fields.description
            if isinstance(self._issue.fields.description, str)
            else None
        )
        pipeline_context.raw_result = await run_coding_task(
            self._executor,
            self._settings,
            str(pipeline_context.repo_path),
            self._mapping.clone_url,
            self._mapping.target_branch,
            self._issue.key,
            self._issue.fields.summary,
            description,
            plugins=self._mapping.plugins,
        )
        await self._log.ainfo(
            "coding_complete",
            summary=pipeline_context.raw_result.summary[:200],
        )

    # -- process -----------------------------------------------------------

    async def process(self, pipeline_context: CodingContext) -> None:
        """Apply patch, commit, push, create MR, update Jira."""
        raw_result = stage_requires(pipeline_context.raw_result, "raw_result")
        repo_path = stage_requires(pipeline_context.repo_path, "repo_path")

        issue = self._issue
        mapping = self._mapping

        await apply_coding_result(raw_result, repo_path)
        has_changes = await git_commit(
            repo_path,
            f"feat({issue.key.lower()}): {issue.fields.summary}",
            self._settings.agent_author_name,
            self._settings.agent_author_email,
        )
        if not has_changes:
            await self._jira.add_comment(issue.key, "Agent found no changes to make.")
            await self._log.awarn("no_changes_to_commit")
            pipeline_context.outcome = "no_changes"
            return

        await git_push(
            repo_path,
            "origin",
            pipeline_context.branch,
            mapping.token,
        )
        mr_title = f"feat({issue.key.lower()}): {issue.fields.summary}"
        mr_desc = (
            f"Automated implementation for {issue.key}.\n\n{strip_json_block(raw_result.summary)}"
        )
        mr_iid = await self._gl.create_merge_request(
            mapping.gitlab_project_id,
            pipeline_context.branch,
            mapping.target_branch,
            mr_title,
            mr_desc,
        )
        mr_url = (
            f"{self._settings.gitlab_url}/{mapping.gitlab_project_id}/-/merge_requests/{mr_iid}"
        )
        await self._jira.add_comment(issue.key, f"MR created: {mr_url}")
        await self._transition_to_in_review(issue.key)
        await self._log.ainfo("coding_task_complete", mr_iid=mr_iid)

    async def _transition_to_in_review(self, issue_key: str) -> None:
        """Transition issue to 'In Review' after MR creation. Non-blocking on failure."""
        in_review = self._mapping.in_review_status
        try:
            await self._jira.transition_issue(issue_key, in_review)
        except Exception:
            await self._log.awarning(
                "in_review_transition_failed",
                issue_key=issue_key,
                target_status=in_review,
            )

    # -- cleanup -----------------------------------------------------------

    async def cleanup(self, pipeline_context: CodingContext) -> None:
        """Remove cloned repo and record metrics."""
        if pipeline_context.repo_path:
            await asyncio.to_thread(shutil.rmtree, pipeline_context.repo_path, True)
        coding_tasks_total.add(1, {"outcome": pipeline_context.outcome})
        coding_tasks_duration.record(
            time.monotonic() - pipeline_context.start_time,
            {"outcome": pipeline_context.outcome},
        )

    # -- error handling ----------------------------------------------------

    async def handle_error(self, pipeline_context: CodingContext, exc: Exception) -> None:
        """Post failure comment to Jira."""
        issue_key = self._issue.key

        if isinstance(exc, TransientCloneError):
            await self._log.aexception("coding_task_transient_failure")
            pipeline_context.outcome = "transient_failure"
            pipeline_context.suppress_exception = True
            try:
                await self._jira.add_comment(
                    issue_key,
                    f"⚠️ Git clone failed after {exc.attempts} attempts "
                    f"due to a transient error.\n\n"
                    f"**Error:** {user_error_message(str(exc))}\n\n"
                    f"This issue has been left in '{self._mapping.in_progress_status}'. "
                    f"The agent will retry on the next poll cycle.",
                )
            except Exception:
                await self._log.aexception("failure_comment_post_failed")
            return

        if isinstance(exc, TaskExecutionError):
            pipeline_context.outcome = "task_failure"

        await post_pipeline_error(
            self._log,
            exc,
            lambda msg: self._jira.add_comment(issue_key, msg),
            task_error_prefix="⚠️ Automated implementation failed.",
            generic_msg="⚠️ Automated implementation failed. Check service logs for details.",
        )
