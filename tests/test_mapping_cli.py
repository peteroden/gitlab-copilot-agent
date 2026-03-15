"""Tests for the mapping-helper CLI — validate, show, render-json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from gitlab_copilot_agent.mapping_cli import main

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JIRA_KEY_PROJ = "PROJ"
JIRA_KEY_OPS = "OPS"
REPO_SERVICE_A = "group/service-a"
REPO_PLATFORM = "group/platform-tools"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_yaml() -> dict[str, object]:
    return {
        "defaults": {"target_branch": "main", "credential_ref": "default"},
        "bindings": [
            {"jira_project": JIRA_KEY_PROJ, "repo": REPO_SERVICE_A},
            {
                "jira_project": JIRA_KEY_OPS,
                "repo": REPO_PLATFORM,
                "target_branch": "develop",
                "credential_ref": "platform_team",
            },
        ],
    }


@pytest.fixture()
def valid_yaml_file(tmp_path: Path) -> Path:
    p = tmp_path / "mappings.yaml"
    p.write_text(yaml.dump(_valid_yaml()))
    return p


@pytest.fixture()
def invalid_yaml_file(tmp_path: Path) -> Path:
    """YAML with a duplicate Jira key."""
    data = {
        "bindings": [
            {"jira_project": JIRA_KEY_PROJ, "repo": REPO_SERVICE_A},
            {"jira_project": JIRA_KEY_PROJ, "repo": REPO_PLATFORM},
        ],
    }
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.dump(data))
    return p


@pytest.fixture()
def bad_repo_yaml_file(tmp_path: Path) -> Path:
    """YAML with a repo path missing a slash."""
    data = {
        "bindings": [
            {"jira_project": JIRA_KEY_PROJ, "repo": "noslash"},
        ],
    }
    p = tmp_path / "bad_repo.yaml"
    p.write_text(yaml.dump(data))
    return p


@pytest.fixture()
def not_a_mapping_yaml(tmp_path: Path) -> Path:
    """YAML that parses to a list instead of a dict."""
    p = tmp_path / "list.yaml"
    p.write_text("- item1\n- item2\n")
    return p


# ---------------------------------------------------------------------------
# validate command
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_file(self, valid_yaml_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["validate", str(valid_yaml_file)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "VALID" in out
        assert "2 bindings" in out
        assert "default" in out
        assert "platform_team" in out

    def test_duplicate_keys(
        self, invalid_yaml_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["validate", str(invalid_yaml_file)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "INVALID" in err
        assert "Duplicate" in err

    def test_bad_repo_path(
        self, bad_repo_yaml_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["validate", str(bad_repo_yaml_file)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "must contain at least one '/'" in err

    def test_not_a_mapping(
        self, not_a_mapping_yaml: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["validate", str(not_a_mapping_yaml)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "expected a YAML mapping" in err

    def test_missing_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["validate", "/nonexistent/file.yaml"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "not a file" in err


# ---------------------------------------------------------------------------
# show command
# ---------------------------------------------------------------------------


class TestShow:
    def test_show_table(self, valid_yaml_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["show", str(valid_yaml_file)])
        assert rc == 0
        out = capsys.readouterr().out
        assert JIRA_KEY_PROJ in out
        assert JIRA_KEY_OPS in out
        assert REPO_SERVICE_A in out
        assert REPO_PLATFORM in out
        assert "develop" in out
        assert "platform_team" in out

    def test_show_invalid(
        self, invalid_yaml_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["show", str(invalid_yaml_file)])
        assert rc == 1


# ---------------------------------------------------------------------------
# render-json command
# ---------------------------------------------------------------------------


class TestRenderJson:
    def test_render_json_output(
        self, valid_yaml_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["render-json", str(valid_yaml_file)])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert JIRA_KEY_PROJ in data["mappings"]
        assert JIRA_KEY_OPS in data["mappings"]
        proj = data["mappings"][JIRA_KEY_PROJ]
        assert proj["repo"] == REPO_SERVICE_A
        assert proj["target_branch"] == "main"
        assert proj["credential_ref"] == "default"
        ops = data["mappings"][JIRA_KEY_OPS]
        assert ops["target_branch"] == "develop"
        assert ops["credential_ref"] == "platform_team"

    def test_render_json_invalid(
        self, invalid_yaml_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["render-json", str(invalid_yaml_file)])
        assert rc == 1
