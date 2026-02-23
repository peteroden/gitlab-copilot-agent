"""Coding orchestrator — Jira issue → clone → code → MR → update."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import structlog

from gitlab_copilot_agent.coding_engine import run_coding_task
from gitlab_copilot_agent.coding_workflow import apply_coding_result
from gitlab_copilot_agent.concurrency import DistributedLock, MemoryLock, ProcessedIssueTracker
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.git_operations import (
    git_clone,
    git_commit,
    git_push,
    git_unique_branch,
)
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.jira_client import JiraClient
from gitlab_copilot_agent.jira_models import JiraIssue
from gitlab_copilot_agent.metrics import coding_tasks_duration, coding_tasks_total
from gitlab_copilot_agent.project_mapping import GitLabProjectMapping
from gitlab_copilot_agent.task_executor import TaskExecutor
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)

AGENT_AUTHOR_NAME = "Copilot Agent"
AGENT_AUTHOR_EMAIL = "copilot-agent@noreply.gitlab.com"


class CodingOrchestrator:
    def __init__(
        self,
        settings: Settings,
        gitlab: GitLabClient,
        jira: JiraClient,
        executor: TaskExecutor,
        repo_locks: DistributedLock | None = None,
        tracker: ProcessedIssueTracker | None = None,
    ) -> None:
        self._settings = settings
        self._gitlab = gitlab
        self._jira = jira
        self._executor = executor
        self._repo_locks = repo_locks or MemoryLock()
        self._tracker = tracker or ProcessedIssueTracker()

    async def _transition_to_in_review(
        self, issue_key: str, bound_log: structlog.stdlib.BoundLogger
    ) -> None:
        """Transition issue to 'In Review' after MR creation. Non-blocking on failure."""
        in_review = self._settings.jira.in_review_status if self._settings.jira else "In Review"
        try:
            await self._jira.transition_issue(issue_key, in_review)
        except Exception:
            await bound_log.awarning(
                "in_review_transition_failed", issue_key=issue_key, target_status=in_review
            )

    async def handle(self, issue: JiraIssue, project_mapping: GitLabProjectMapping) -> None:
        if self._tracker.is_processed(issue.key):
            return

        start = time.monotonic()
        outcome = "error"
        with _tracer.start_as_current_span(
            "jira.coding_task",
            attributes={"jira_key": issue.key, "project_id": project_mapping.gitlab_project_id},
        ):
            async with self._repo_locks.acquire(project_mapping.clone_url):
                bound_log = log.bind(
                    issue_key=issue.key, project_id=project_mapping.gitlab_project_id
                )
                await bound_log.ainfo("coding_task_started")
                description = (
                    issue.fields.description if isinstance(issue.fields.description, str) else None
                )
                repo_path: Path | None = None
                try:
                    in_prog = (
                        self._settings.jira.in_progress_status
                        if self._settings.jira
                        else "In Progress"
                    )
                    await self._jira.transition_issue(issue.key, in_prog)
                    repo_path = await git_clone(
                        project_mapping.clone_url,
                        project_mapping.target_branch,
                        self._settings.gitlab_token,
                        clone_dir=self._settings.clone_dir,
                    )
                    branch = await git_unique_branch(repo_path, f"agent/{issue.key.lower()}")
                    result = await run_coding_task(
                        self._executor,
                        self._settings,
                        str(repo_path),
                        project_mapping.clone_url,
                        project_mapping.target_branch,
                        issue.key,
                        issue.fields.summary,
                        description,
                    )
                    await bound_log.ainfo("coding_complete", summary=result.summary[:200])
                    await apply_coding_result(result, repo_path)
                    has_changes = await git_commit(
                        repo_path,
                        f"feat({issue.key.lower()}): {issue.fields.summary}",
                        AGENT_AUTHOR_NAME,
                        AGENT_AUTHOR_EMAIL,
                    )
                    if not has_changes:
                        await self._jira.add_comment(issue.key, "Agent found no changes to make.")
                        await bound_log.awarn("no_changes_to_commit")
                        outcome = "no_changes"
                        return
                    await git_push(
                        repo_path,
                        "origin",
                        branch,
                        self._settings.gitlab_token,
                    )
                    mr_title = f"feat({issue.key.lower()}): {issue.fields.summary}"
                    mr_desc = f"Automated implementation for {issue.key}.\n\n{result.summary}"
                    mr_iid = await self._gitlab.create_merge_request(
                        project_mapping.gitlab_project_id,
                        branch,
                        project_mapping.target_branch,
                        mr_title,
                        mr_desc,
                    )
                    mr_url = (
                        f"{self._settings.gitlab_url}"
                        f"/{project_mapping.gitlab_project_id}"
                        f"/-/merge_requests/{mr_iid}"
                    )
                    await self._jira.add_comment(issue.key, f"MR created: {mr_url}")
                    await self._transition_to_in_review(issue.key, bound_log)
                    await bound_log.ainfo("coding_task_complete", mr_iid=mr_iid)
                    self._tracker.mark(issue.key)
                    outcome = "success"
                except Exception:
                    await bound_log.aexception("coding_task_failed")
                    try:
                        await self._jira.add_comment(
                            issue.key,
                            "⚠️ Automated implementation failed. Check service logs for details.",
                        )
                    except Exception:
                        await bound_log.aexception("failure_comment_post_failed")
                    raise
                finally:
                    if repo_path:
                        await asyncio.to_thread(shutil.rmtree, repo_path, True)
                    coding_tasks_total.add(1, {"outcome": outcome})
                    coding_tasks_duration.record(time.monotonic() - start, {"outcome": outcome})
