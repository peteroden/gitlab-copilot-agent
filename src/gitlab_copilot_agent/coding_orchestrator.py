"""Coding orchestrator — Jira issue → clone → code → MR → update."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import structlog

from gitlab_copilot_agent.coding_engine import run_coding_task
from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.git_operations import git_clone, git_commit, git_create_branch, git_push
from gitlab_copilot_agent.gitlab_client import GitLabClient
from gitlab_copilot_agent.jira_client import JiraClient
from gitlab_copilot_agent.jira_models import JiraIssue
from gitlab_copilot_agent.project_mapping import GitLabProjectMapping

log = structlog.get_logger()

AGENT_AUTHOR_NAME = "Copilot Agent"
AGENT_AUTHOR_EMAIL = "copilot-agent@noreply.gitlab.com"


class CodingOrchestrator:
    def __init__(self, settings: Settings, gitlab: GitLabClient, jira: JiraClient) -> None:
        self._settings = settings
        self._gitlab = gitlab
        self._jira = jira

    async def handle(self, issue: JiraIssue, project_mapping: GitLabProjectMapping) -> None:
        bound_log = log.bind(issue_key=issue.key, project_id=project_mapping.gitlab_project_id)
        await bound_log.ainfo("coding_task_started")
        description = issue.fields.description if isinstance(issue.fields.description, str) else None
        repo_path: Path | None = None
        try:
            in_prog = self._settings.jira.in_progress_status if self._settings.jira else "In Progress"
            await self._jira.transition_issue(issue.key, in_prog)
            repo_path = await git_clone(project_mapping.clone_url, project_mapping.target_branch, self._settings.gitlab_token)
            await git_create_branch(repo_path, f"agent/{issue.key.lower()}")
            result = await run_coding_task(self._settings, str(repo_path), issue.key, issue.fields.summary, description)
            await bound_log.ainfo("coding_complete", summary=result[:200])
            has_changes = await git_commit(repo_path, f"feat({issue.key.lower()}): {issue.fields.summary}", AGENT_AUTHOR_NAME, AGENT_AUTHOR_EMAIL)
            if not has_changes:
                await self._jira.add_comment(issue.key, "Agent found no changes to make.")
                await bound_log.awarn("no_changes_to_commit")
                return
            await git_push(repo_path, "origin", f"agent/{issue.key.lower()}", self._settings.gitlab_token)
            mr_title = f"feat({issue.key.lower()}): {issue.fields.summary}"
            mr_desc = f"Automated implementation for {issue.key}.\n\n{result}"
            mr_iid = await self._gitlab.create_merge_request(project_mapping.gitlab_project_id, f"agent/{issue.key.lower()}", project_mapping.target_branch, mr_title, mr_desc)
            mr_url = f"{self._settings.gitlab_url}/{project_mapping.gitlab_project_id}/-/merge_requests/{mr_iid}"
            await self._jira.add_comment(issue.key, f"MR created: {mr_url}")
            await bound_log.ainfo("coding_task_complete", mr_iid=mr_iid)
        except Exception:
            await bound_log.aexception("coding_task_failed")
            raise
        finally:
            if repo_path:
                await asyncio.to_thread(shutil.rmtree, repo_path, True)
