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
from gitlab_copilot_agent.project_mapping import GitLabProjectMapping, ProjectMap
from gitlab_copilot_agent.telemetry import get_tracer

log = structlog.get_logger()
_tracer = get_tracer(__name__)


class CodingTaskHandler(Protocol):
    """Interface for handling discovered coding tasks."""

    async def handle(self, issue: JiraIssue, project_mapping: GitLabProjectMapping) -> None: ...


class JiraPoller:
    """Background poller that searches Jira for issues and invokes a handler."""

    def __init__(
        self,
        jira_client: JiraClient,
        settings: JiraSettings,
        project_map: ProjectMap,
        handler: CodingTaskHandler,
    ) -> None:
        self._client = jira_client
        self._trigger_status = settings.trigger_status
        self._interval = settings.poll_interval
        self._project_map = project_map
        self._handler = handler
        self._task: asyncio.Task[None] | None = None
        self._processed_issues: set[str] = set()

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
        with _tracer.start_as_current_span("jira.poll"):
            # Build JQL for all projects in the map
            project_keys = list(self._project_map.mappings.keys())
            if not project_keys:
                return

            project_list = ", ".join(f'"{key}"' for key in project_keys)
            jql = f'status = "{self._trigger_status}" AND project IN ({project_list})'

            issues = await self._client.search_issues(jql)
            span = trace.get_current_span()
            span.set_attribute("issue_count", len(issues))

            for issue in issues:
                # Skip if already processed in this session
                if issue.key in self._processed_issues:
                    continue

                mapping = self._project_map.get(issue.project_key)
                if mapping:
                    await self._handler.handle(issue, mapping)
                    self._processed_issues.add(issue.key)
