"""Tests for the GitLab provisioner module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import gitlab.exceptions
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from demo_provision.gitlab_provisioner import (
    create_project,
    create_webhook,
    get_namespace,
    get_project,
    load_template,
    push_files,
)

from .conftest import (
    GITLAB_GROUP,
    GITLAB_PROJECT_NAME,
    GITLAB_PROJECT_PATH,
    TEMPLATE_DIR,
)


class TestGetProject:
    def test_returns_project_when_exists(self, mock_gl: MagicMock) -> None:
        mock_project = MagicMock()
        mock_project.id = 42
        mock_gl.projects.get.return_value = mock_project

        result = get_project(mock_gl, GITLAB_PROJECT_PATH)

        assert result is not None
        assert result.id == 42
        mock_gl.projects.get.assert_called_once_with(GITLAB_PROJECT_PATH)

    def test_returns_none_when_not_found(self, mock_gl: MagicMock) -> None:
        mock_gl.projects.get.side_effect = gitlab.exceptions.GitlabGetError

        result = get_project(mock_gl, GITLAB_PROJECT_PATH)

        assert result is None


class TestCreateProject:
    def test_creates_project_with_correct_params(self, mock_gl: MagicMock) -> None:
        mock_project = MagicMock()
        mock_project.id = 99
        mock_project.path_with_namespace = GITLAB_PROJECT_PATH
        mock_gl.projects.create.return_value = mock_project

        result = create_project(mock_gl, GITLAB_PROJECT_NAME, 10)

        mock_gl.projects.create.assert_called_once_with(
            {
                "name": GITLAB_PROJECT_NAME,
                "namespace_id": 10,
                "visibility": "private",
                "description": "",
                "initialize_with_readme": True,
            }
        )
        assert result.id == 99

    def test_creates_project_with_custom_visibility(self, mock_gl: MagicMock) -> None:
        mock_gl.projects.create.return_value = MagicMock()

        create_project(mock_gl, GITLAB_PROJECT_NAME, 10, visibility="public")

        create_call = mock_gl.projects.create.call_args[0][0]
        assert create_call["visibility"] == "public"


class TestGetNamespace:
    def test_returns_group(self, mock_gl: MagicMock) -> None:
        mock_group = MagicMock()
        mock_group.id = 5
        mock_gl.groups.get.return_value = mock_group

        result = get_namespace(mock_gl, GITLAB_GROUP)

        assert result.id == 5

    def test_exits_when_group_not_found(self, mock_gl: MagicMock) -> None:
        mock_gl.groups.get.side_effect = gitlab.exceptions.GitlabGetError

        with pytest.raises(SystemExit):
            get_namespace(mock_gl, "nonexistent-group")


class TestPushFiles:
    def test_pushes_files_as_create_actions(self, mock_project: MagicMock) -> None:
        files = {"main.py": "print('hello')", "README.md": "# Demo"}

        push_files(mock_project, "main", files, "Initial commit")

        mock_project.commits.create.assert_called_once()
        commit_data = mock_project.commits.create.call_args[0][0]
        assert commit_data["branch"] == "main"
        assert commit_data["commit_message"] == "Initial commit"
        assert len(commit_data["actions"]) == 2
        assert all(a["action"] == "create" for a in commit_data["actions"])


class TestCreateWebhook:
    def test_creates_webhook_with_correct_params(self, mock_project: MagicMock) -> None:
        create_webhook(mock_project, "https://example.com/webhook", "secret123")

        mock_project.hooks.create.assert_called_once_with(
            {
                "url": "https://example.com/webhook",
                "token": "secret123",
                "merge_requests_events": True,
                "note_events": True,
                "push_events": False,
                "enable_ssl_verification": True,
            }
        )


class TestLoadTemplate:
    def test_loads_all_template_files(self) -> None:
        files = load_template(TEMPLATE_DIR)

        assert len(files) >= 10
        assert "src/demo_app/main.py" in files
        assert "src/demo_app/database.py" in files
        assert "src/demo_app/auth.py" in files
        assert "AGENTS.md" in files
        assert ".github/copilot-instructions.md" in files
        assert ".github/skills/security-patterns/SKILL.md" in files
        assert ".github/agents/security-reviewer.agent.md" in files

    def test_template_files_are_non_empty(self) -> None:
        files = load_template(TEMPLATE_DIR)

        for path, content in files.items():
            if path.endswith("__init__.py"):
                continue
            assert content.strip(), f"{path} is empty"
