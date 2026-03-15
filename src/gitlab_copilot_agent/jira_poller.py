"""Background Jira poller — discovers issues and invokes a handler."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Protocol

import structlog
from opentelemetry import trace

from gitlab_copilot_agent.config import JiraSettings
from gitlab_copilot_agent.jira_client import JiraClient
from gitlab_copilot_agent.jira_models import JiraIssue
from gitlab_copilot_agent.project_registry import ProjectRegistry, ResolvedProject
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)


class CodingTaskHandler(Protocol):
    """Interface for handling discovered coding tasks."""

    async def handle(self, issue: JiraIssue, project_mapping: ResolvedProject) -> None: ...


class JiraPoller:
    """Background poller that searches Jira for issues and invokes a handler."""

    def __init__(
        self,
        jira_client: JiraClient,
        settings: JiraSettings,
        project_map: ProjectRegistry,
        handler: CodingTaskHandler,
        allowed_project_ids: set[int] | None = None,
    ) -> None:
        self._client = jira_client
        self._trigger_status = settings.trigger_status
        self._interval = settings.poll_interval
        self._project_map = project_map
        self._handler = handler
        self._allowed_project_ids = allowed_project_ids
        self._task: asyncio.Task[None] | None = None
        self._processed_issues: set[str] = set()
        self._poll_lock = asyncio.Lock()

    async def reload_registry(self, registry: ProjectRegistry) -> None:
        """Swap the project registry atomically between poll cycles."""
        async with self._poll_lock:
            old_keys = self._project_map.jira_keys()
            self._project_map = registry
            self._processed_issues.clear()
            new_keys = registry.jira_keys()
            await log.awarn(
                "registry_reloaded",
                added=sorted(new_keys - old_keys),
                removed=sorted(old_keys - new_keys),
                processed_issues_cleared=True,
            )

    async def start(self) -> None:
        """Start the polling loop as a background task."""
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the polling loop."""
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _poll_loop(self) -> None:
        """Poll Jira on interval, invoke handler for each discovered issue."""
        while True:
            try:
                await self._poll_once()
            except Exception:
                await log.aexception("jira_poll_error")
            await asyncio.sleep(self._interval)

    async def _poll_once(self) -> None:
        """Single poll cycle — search for issues, filter by project map, invoke handler."""
        async with self._poll_lock:
            with _tracer.start_as_current_span("jira.poll"):
                project_keys = sorted(self._project_map.jira_keys())
                if not project_keys:
                    return

                project_list = ", ".join(f'"{key}"' for key in project_keys)
                jql = f'status = "{self._trigger_status}" AND project IN ({project_list})'

                issues = await self._client.search_issues(jql)
                span = trace.get_current_span()
                span.set_attribute("issue_count", len(issues))

                for issue in issues:
                    if issue.key in self._processed_issues:
                        continue

                    mapping = self._project_map.get_by_jira(issue.project_key)
                    if mapping:
                        if (
                            self._allowed_project_ids is not None
                            and mapping.gitlab_project_id not in self._allowed_project_ids
                        ):
                            await log.awarn(
                                "jira_task_skipped",
                                issue=issue.key,
                                gitlab_project_id=mapping.gitlab_project_id,
                                reason="project_not_in_allowlist",
                            )
                            continue
                        await self._handler.handle(issue, mapping)
                        self._processed_issues.add(issue.key)
