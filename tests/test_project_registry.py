from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.credential_registry import CredentialRegistry
from gitlab_copilot_agent.mapping_models import RenderedBinding, RenderedMap
from gitlab_copilot_agent.project_registry import ProjectRegistry, ResolvedProject

GITLAB_URL = "https://gitlab.example.com"
DEFAULT_TOKEN = "glpat-default-token"  # noqa: S105
PLATFORM_TOKEN = "glpat-platform-token"  # noqa: S105
JIRA_PROJ = "PROJ"
JIRA_OPS = "OPS"
REPO_A = "group/service-a"
REPO_B = "group/platform-tools"
PID_A, PID_B = 42, 99


def _proj(
    jira: str = JIRA_PROJ,
    repo: str = REPO_A,
    pid: int = PID_A,
    branch: str = "main",
    cred: str = "default",
    token: str = DEFAULT_TOKEN,
) -> ResolvedProject:
    return ResolvedProject(
        jira_project=jira,
        repo=repo,
        gitlab_project_id=pid,
        clone_url=f"{GITLAB_URL}/{repo}.git",
        target_branch=branch,
        credential_ref=cred,
        token=token,
    )


def _registry() -> ProjectRegistry:
    return ProjectRegistry(
        [
            _proj(),
            _proj(
                jira=JIRA_OPS,
                repo=REPO_B,
                pid=PID_B,
                branch="develop",
                cred="platform_team",
                token=PLATFORM_TOKEN,
            ),
        ]
    )


class TestLookup:
    def test_get_by_jira(self) -> None:
        assert _registry().get_by_jira(JIRA_PROJ) is not None

    def test_get_by_jira_missing(self) -> None:
        assert _registry().get_by_jira("MISSING") is None

    def test_get_by_project_id(self) -> None:
        p = _registry().get_by_project_id(PID_B)
        assert p is not None and p.jira_project == JIRA_OPS

    def test_get_by_project_id_missing(self) -> None:
        assert _registry().get_by_project_id(999) is None

    def test_jira_keys(self) -> None:
        assert _registry().jira_keys() == {JIRA_PROJ, JIRA_OPS}

    def test_duplicate_project_id_raises(self) -> None:
        with pytest.raises(ValueError, match="Duplicate gitlab_project_id"):
            ProjectRegistry([_proj(jira="A"), _proj(jira="B")])

    def test_repr_hides_token(self) -> None:
        r = repr(_proj())
        assert DEFAULT_TOKEN not in r
        assert "token" not in r


def _binding(repo: str = REPO_A, branch: str = "main", cred: str = "default") -> RenderedBinding:
    return RenderedBinding(repo=repo, target_branch=branch, credential_ref=cred)


class TestFromRenderedMap:
    async def test_resolves_with_multi_credential(self) -> None:
        rendered = RenderedMap(
            mappings={
                JIRA_PROJ: _binding(),
                JIRA_OPS: _binding(repo=REPO_B, branch="develop", cred="platform_team"),
            }
        )
        creds = CredentialRegistry(
            default_token=DEFAULT_TOKEN,
            named_tokens={"platform_team": PLATFORM_TOKEN},
        )
        with patch("gitlab_copilot_agent.project_registry.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value = AsyncMock(
                resolve_project=AsyncMock(side_effect=[PID_A, PID_B]),
            )
            reg = await ProjectRegistry.from_rendered_map(rendered, creds, GITLAB_URL)
        p = reg.get_by_jira(JIRA_PROJ)
        assert p is not None and p.gitlab_project_id == PID_A and p.token == DEFAULT_TOKEN
        assert p.clone_url == f"https://gitlab.example.com/{REPO_A}.git"
        o = reg.get_by_jira(JIRA_OPS)
        assert o is not None and o.token == PLATFORM_TOKEN

    async def test_unknown_credential_ref_raises(self) -> None:
        rendered = RenderedMap(mappings={JIRA_PROJ: _binding(cred="nonexistent")})
        creds = CredentialRegistry(default_token=DEFAULT_TOKEN)
        with pytest.raises(KeyError, match="Unknown credential_ref"):
            await ProjectRegistry.from_rendered_map(rendered, creds, GITLAB_URL)


class TestOptionalJiraProject:
    """Tests for optional jira_project (review-only projects)."""

    def test_none_jira_project_not_indexed(self) -> None:
        proj = _proj(jira="PROJ")
        no_jira = ResolvedProject(
            repo="group/review-only",
            gitlab_project_id=200,
            clone_url=f"{GITLAB_URL}/group/review-only.git",
            target_branch="main",
            credential_ref="default",
            token=DEFAULT_TOKEN,
        )
        reg = ProjectRegistry([proj, no_jira])
        assert reg.jira_keys() == {JIRA_PROJ}
        assert reg.get_by_project_id(200) is not None
        assert reg.get_by_jira("PROJ") is not None

    def test_all_none_jira_produces_empty_jira_keys(self) -> None:
        no_jira = ResolvedProject(
            repo="group/review-only",
            gitlab_project_id=200,
            clone_url=f"{GITLAB_URL}/group/review-only.git",
            target_branch="main",
            credential_ref="default",
            token=DEFAULT_TOKEN,
        )
        reg = ProjectRegistry([no_jira])
        assert reg.jira_keys() == set()
        assert reg.get_by_project_id(200) is not None


class TestFromConfig:
    """Tests for ProjectRegistry.from_config() with v2 ConfigFile."""

    async def test_builds_from_v2_config(self) -> None:
        from gitlab_copilot_agent.config_v2 import ConfigFile

        config = ConfigFile.model_validate(
            {
                "version": 2,
                "gitlab": {"url": GITLAB_URL},
                "projects": [
                    {"repo": REPO_A},
                    {"repo": REPO_B, "credential_ref": "platform_team"},
                ],
            }
        )
        creds = CredentialRegistry(
            default_token=DEFAULT_TOKEN,
            named_tokens={"platform_team": PLATFORM_TOKEN},
        )
        with patch("gitlab_copilot_agent.project_registry.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value = AsyncMock(
                resolve_project=AsyncMock(side_effect=[PID_A, PID_B]),
            )
            reg = await ProjectRegistry.from_config(config, creds, GITLAB_URL)

        assert reg.get_by_project_id(PID_A) is not None
        assert reg.get_by_project_id(PID_B) is not None
        # No jira integrations → jira_project should be None
        assert reg.get_by_project_id(PID_A).jira_project is None
        assert reg.jira_keys() == set()

    async def test_resolves_jira_integration(self) -> None:
        from gitlab_copilot_agent.config_v2 import ConfigFile

        config = ConfigFile.model_validate(
            {
                "version": 2,
                "gitlab": {"url": GITLAB_URL},
                "projects": [
                    {"repo": REPO_A, "integrations": ["my-jira"]},
                ],
                "integrations": [
                    {"name": "my-jira", "type": "jira", "project_key": "PROJ"},
                ],
            }
        )
        creds = CredentialRegistry(default_token=DEFAULT_TOKEN)
        with patch("gitlab_copilot_agent.project_registry.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value = AsyncMock(
                resolve_project=AsyncMock(return_value=PID_A),
            )
            reg = await ProjectRegistry.from_config(config, creds, GITLAB_URL)

        p = reg.get_by_project_id(PID_A)
        assert p is not None
        assert p.jira_project == "PROJ"
        assert reg.jira_keys() == {"PROJ"}
