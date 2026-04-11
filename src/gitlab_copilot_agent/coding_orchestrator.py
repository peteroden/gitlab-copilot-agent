"""Coding orchestrator — Jira issue → clone → code → MR → update.

Thin delegation layer: checks dedup, acquires repo lock, constructs
the CodingPipeline, and calls ``run_pipeline()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from gitlab_copilot_agent.coding_pipeline import CodingContext, CodingPipeline
from gitlab_copilot_agent.concurrency import DistributedLock, MemoryLock
from gitlab_copilot_agent.pipeline import run_pipeline
from gitlab_copilot_agent.telemetry import get_tracer

if TYPE_CHECKING:
    from gitlab_copilot_agent.config import Settings
    from gitlab_copilot_agent.dedup import DeduplicationService
    from gitlab_copilot_agent.gitlab_client import GitLabClient
    from gitlab_copilot_agent.jira_client import JiraClient
    from gitlab_copilot_agent.jira_models import JiraIssue
    from gitlab_copilot_agent.project_registry import ResolvedProject
    from gitlab_copilot_agent.task_executor import TaskExecutor

log = structlog.get_logger()
_tracer = get_tracer(__name__)


class CodingOrchestrator:
    """Orchestrates Jira coding tasks through the CodingPipeline."""

    def __init__(
        self,
        settings: Settings,
        gitlab: GitLabClient,
        jira: JiraClient,
        executor: TaskExecutor,
        repo_locks: DistributedLock | None = None,
        dedup: DeduplicationService | None = None,
    ) -> None:
        self._settings = settings
        self._gitlab = gitlab
        self._jira = jira
        self._executor = executor
        self._repo_locks = repo_locks or MemoryLock()
        self._dedup = dedup

    async def handle(self, issue: JiraIssue, project_mapping: ResolvedProject) -> None:
        """Handle a Jira coding task through the pipeline protocol."""
        if self._dedup is not None and await self._dedup.is_issue_seen(issue.key):
            return

        with _tracer.start_as_current_span(
            "jira.coding_task",
            attributes={
                "jira_key": issue.key,
                "project_id": project_mapping.gitlab_project_id,
            },
        ):
            async with self._repo_locks.acquire(project_mapping.clone_url):
                pipeline = CodingPipeline(
                    settings=self._settings,
                    issue=issue,
                    project_mapping=project_mapping,
                    executor=self._executor,
                    gitlab_client=self._gitlab,
                    jira_client=self._jira,
                )
                pipeline_context = CodingContext()
                await run_pipeline(pipeline, pipeline_context)

                if self._dedup is not None and pipeline_context.outcome == "success":
                    await self._dedup.mark_issue(issue.key)
