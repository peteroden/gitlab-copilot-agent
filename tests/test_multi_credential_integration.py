"""Integration test — multi-credential flow through webhook → orchestrator.

Verifies that webhooks for two different projects use separate per-project
tokens (from the ProjectRegistry) throughout the full pipeline: webhook
resolution, GitLabClient construction, and clone_repo call.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient

from gitlab_copilot_agent.discussion_models import AgentIdentity
from gitlab_copilot_agent.gitlab_client import MRDetails
from gitlab_copilot_agent.main import app
from gitlab_copilot_agent.project_registry import ProjectRegistry, ResolvedProject
from gitlab_copilot_agent.task_executor import ReviewResult
from tests.conftest import (
    DIFF_REFS,
    FAKE_REVIEW_OUTPUT,
    GITLAB_URL,
    HEADERS,
    make_settings,
)

# -- Two projects with distinct credentials --

PROJECT_ALPHA_ID = 100
PROJECT_ALPHA_TOKEN = "token-alpha"
PROJECT_ALPHA_REPO = "team-alpha/service-a"

PROJECT_BETA_ID = 200
PROJECT_BETA_TOKEN = "token-beta"
PROJECT_BETA_REPO = "team-beta/service-b"

AGENT_USER_ID = 999
AGENT_USERNAME = "copilot-bot"


def _make_multi_project_registry() -> ProjectRegistry:
    return ProjectRegistry(
        [
            ResolvedProject(
                jira_project="ALPHA",
                repo=PROJECT_ALPHA_REPO,
                gitlab_project_id=PROJECT_ALPHA_ID,
                clone_url=f"{GITLAB_URL}/{PROJECT_ALPHA_REPO}.git",
                target_branch="main",
                credential_ref="alpha-cred",
                token=PROJECT_ALPHA_TOKEN,
            ),
            ResolvedProject(
                jira_project="BETA",
                repo=PROJECT_BETA_REPO,
                gitlab_project_id=PROJECT_BETA_ID,
                clone_url=f"{GITLAB_URL}/{PROJECT_BETA_REPO}.git",
                target_branch="develop",
                credential_ref="beta-cred",
                token=PROJECT_BETA_TOKEN,
            ),
        ]
    )


def _mr_payload(project_id: int, repo: str, mr_iid: int = 1) -> dict[str, object]:
    """Build a minimal MR webhook payload for a given project."""
    return {
        "object_kind": "merge_request",
        "user": {"id": 1, "username": "dev"},
        "project": {
            "id": project_id,
            "path_with_namespace": repo,
            "git_http_url": f"https://gitlab.example.com/{repo}.git",
        },
        "object_attributes": {
            "iid": mr_iid,
            "title": f"MR for {repo}",
            "description": "test",
            "action": "open",
            "source_branch": "feature/x",
            "target_branch": "main",
            "last_commit": {"id": f"sha-{project_id}", "message": "feat: change"},
            "url": f"https://gitlab.example.com/{repo}/-/merge_requests/{mr_iid}",
        },
    }


@patch("gitlab_copilot_agent.orchestrator.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
@patch("gitlab_copilot_agent.orchestrator.gitlab.Gitlab")
async def test_multi_project_webhooks_use_distinct_tokens(
    _mock_gl_class: MagicMock,
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    _mock_post_review: AsyncMock,
    client: AsyncClient,
) -> None:
    """Two MR webhooks for different projects each use their own per-project token.

    Uses side_effect to return distinct mock instances per token, so we can
    verify that each project's clone_repo call receives the matching token
    (not just that both tokens appeared somewhere).
    """
    # Track which mock instance was created for which token
    instances_by_token: dict[str, MagicMock] = {}

    def _make_client(url: str, token: str) -> MagicMock:
        instance = MagicMock()
        instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
        instance.cleanup = AsyncMock()
        instance.get_mr_details = AsyncMock(
            return_value=MRDetails(
                title="test", description="test", diff_refs=DIFF_REFS, changes=[]
            )
        )
        instance.post_mr_comment = AsyncMock()
        instances_by_token[token] = instance
        return instance

    mock_client_class.side_effect = _make_client
    mock_run_review.return_value = ReviewResult(summary=FAKE_REVIEW_OUTPUT)

    registry = _make_multi_project_registry()
    app.state.project_registry = registry
    app.state.settings = make_settings()

    try:
        # Send webhook for project Alpha
        resp_alpha = await client.post(
            "/webhook",
            json=_mr_payload(PROJECT_ALPHA_ID, PROJECT_ALPHA_REPO),
            headers=HEADERS,
        )
        assert resp_alpha.json() == {"status": "queued"}

        # Send webhook for project Beta
        resp_beta = await client.post(
            "/webhook",
            json=_mr_payload(PROJECT_BETA_ID, PROJECT_BETA_REPO, mr_iid=2),
            headers=HEADERS,
        )
        assert resp_beta.json() == {"status": "queued"}

        # Wait for background tasks
        await asyncio.sleep(0.3)

        # Verify distinct instances were created for each token
        assert set(instances_by_token.keys()) == {PROJECT_ALPHA_TOKEN, PROJECT_BETA_TOKEN}

        # Verify each instance's clone_repo was called with the MATCHING token
        alpha_instance = instances_by_token[PROJECT_ALPHA_TOKEN]
        alpha_instance.clone_repo.assert_awaited_once()
        assert alpha_instance.clone_repo.call_args.args[2] == PROJECT_ALPHA_TOKEN

        beta_instance = instances_by_token[PROJECT_BETA_TOKEN]
        beta_instance.clone_repo.assert_awaited_once()
        assert beta_instance.clone_repo.call_args.args[2] == PROJECT_BETA_TOKEN
    finally:
        app.state.project_registry = None


@patch("gitlab_copilot_agent.orchestrator.post_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.run_review", new_callable=AsyncMock)
@patch("gitlab_copilot_agent.orchestrator.GitLabClient")
@patch("gitlab_copilot_agent.orchestrator.gitlab.Gitlab")
async def test_note_webhook_multi_project_token_isolation(
    _mock_gl_class: MagicMock,
    mock_client_class: MagicMock,
    mock_run_review: AsyncMock,
    _mock_post_review: AsyncMock,
    client: AsyncClient,
) -> None:
    """Discussion webhook for project Beta uses Beta's token, not Alpha's."""
    mock_gl_instance = mock_client_class.return_value
    mock_gl_instance.clone_repo = AsyncMock(return_value="/tmp/fake-repo")
    mock_gl_instance.cleanup = AsyncMock()
    mock_gl_instance.get_mr_details = AsyncMock(
        return_value=MRDetails(title="test", description="test", diff_refs=DIFF_REFS, changes=[])
    )

    registry = _make_multi_project_registry()
    app.state.project_registry = registry

    # Wire credential registry for @mention routing
    mock_cred = MagicMock()
    mock_cred.resolve_identity = AsyncMock(
        return_value=AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
    )
    app.state.credential_registry = mock_cred

    note_payload = {
        "object_kind": "note",
        "user": {"id": 1, "username": "dev"},
        "project": {
            "id": PROJECT_BETA_ID,
            "path_with_namespace": PROJECT_BETA_REPO,
            "git_http_url": f"https://gitlab.example.com/{PROJECT_BETA_REPO}.git",
        },
        "object_attributes": {
            "note": f"@{AGENT_USERNAME} review this",
            "noteable_type": "MergeRequest",
        },
        "merge_request": {
            "iid": 5,
            "title": "Fix beta",
            "source_branch": "fix/beta",
            "target_branch": "develop",
        },
    }

    try:
        with patch(
            "gitlab_copilot_agent.webhook.handle_discussion_interaction", new_callable=AsyncMock
        ) as mock_handle:
            resp = await client.post("/webhook", json=note_payload, headers=HEADERS)
            assert resp.json() == {"status": "queued"}
            await asyncio.sleep(0.1)

            mock_handle.assert_awaited_once()
            _, kwargs = mock_handle.call_args
            assert kwargs["project_token"] == PROJECT_BETA_TOKEN
    finally:
        app.state.project_registry = None
        del app.state.credential_registry
