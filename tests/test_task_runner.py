import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.coding_engine import parse_agent_output
from gitlab_copilot_agent.task_runner import (
    ENV_BRANCH,
    ENV_REPO_URL,
    ENV_TASK_ID,
    ENV_TASK_PAYLOAD,
    ENV_TASK_TYPE,
    _build_coding_result,
    _coding_response_validator,
    _get_required_env,
    _parse_task_payload,
    _validate_repo_url,
    run_task,
)
from tests.conftest import EXAMPLE_CLONE_URL, GITLAB_URL

TASK_ID = "task-001"
PAYLOAD = json.dumps({"prompt": "Review this"})
BAD_HOST = "https://evil.example.com/g/r.git"
_M = "gitlab_copilot_agent.task_runner"


@pytest.fixture()
def task_env(env_vars: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set task-runner env vars on top of the base env_vars fixture."""
    monkeypatch.setenv(ENV_TASK_TYPE, "review")
    monkeypatch.setenv(ENV_TASK_ID, TASK_ID)
    monkeypatch.setenv(ENV_REPO_URL, EXAMPLE_CLONE_URL)
    monkeypatch.setenv(ENV_BRANCH, "feat/x")
    monkeypatch.setenv(ENV_TASK_PAYLOAD, PAYLOAD)


class TestHelpers:
    def test_req_env_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_TV", "x")
        assert _get_required_env("_TV") == "x"

    @pytest.mark.parametrize("val", [None, "  "])
    def test_req_env_fail(self, monkeypatch: pytest.MonkeyPatch, val: str | None) -> None:
        monkeypatch.delenv("_TV", raising=False) if val is None else monkeypatch.setenv("_TV", val)
        with pytest.raises(RuntimeError):
            _get_required_env("_TV")

    @pytest.mark.parametrize(
        ("raw", "match"),
        [("{x", "Invalid JSON"), ("[1]", "JSON object"), ('""', "JSON object")],
    )
    def test_payload_fail(self, raw: str, match: str) -> None:
        with pytest.raises(RuntimeError, match=match):
            _parse_task_payload(raw)

    @pytest.mark.parametrize("url", [EXAMPLE_CLONE_URL, "https://GitLab.Example.COM/g/r.git"])
    def test_validate_url_ok(self, url: str) -> None:
        _validate_repo_url(url, GITLAB_URL)

    def test_validate_url_rejects_different_port(self) -> None:
        with pytest.raises(RuntimeError, match="does not match"):
            _validate_repo_url("https://gitlab.example.com:8443/g/r.git", GITLAB_URL)

    def test_validate_url_rejects_scheme_mismatch(self) -> None:
        with pytest.raises(RuntimeError, match="does not match"):
            _validate_repo_url("http://gitlab.example.com/g/r.git", GITLAB_URL)

    @pytest.mark.parametrize(("url", "match"), [(BAD_HOST, "does not match"), ("x", "no host")])
    def test_validate_url_fail(self, url: str, match: str) -> None:
        with pytest.raises(RuntimeError, match=match):
            _validate_repo_url(url, GITLAB_URL)


class TestRunTask:
    async def test_ok(self, task_env: None) -> None:
        fp = Path("/tmp/fake")
        expected = json.dumps({"result_type": "review", "summary": "done"})
        with (
            patch(f"{_M}.git_clone", AsyncMock(return_value=fp)),
            patch(f"{_M}.run_copilot_session", AsyncMock(return_value="done")),
            patch(f"{_M}._store_result", AsyncMock()) as store,
            patch(f"{_M}.shutil.rmtree") as rm,
        ):
            assert await run_task() == 0
            rm.assert_called_once_with(fp, ignore_errors=True)
            store.assert_awaited_once_with(TASK_ID, expected)

    async def test_missing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_TASK_TYPE, raising=False)
        assert await run_task() == 1

    async def test_bad_type(self, task_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_TASK_TYPE, "bad")
        assert await run_task() == 1

    async def test_url_mismatch(self, task_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_REPO_URL, BAD_HOST)
        with pytest.raises(RuntimeError, match="does not match"):
            await run_task()

    async def test_coding(self, task_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_TASK_TYPE, "coding")
        coding_json = json.dumps(
            {"result_type": "coding", "summary": "x", "patch": "p", "base_sha": "abc"}
        )
        with (
            patch(f"{_M}.git_clone", AsyncMock(return_value=Path("/tmp/r"))),
            patch(f"{_M}.run_copilot_session", AsyncMock(return_value="x")) as ms,
            patch(f"{_M}._build_coding_result", AsyncMock(return_value=coding_json)),
            patch(f"{_M}._store_result", AsyncMock()),
            patch(f"{_M}.shutil.rmtree"),
            patch("gitlab_copilot_agent.coding_engine.ensure_gitignore"),
        ):
            assert await run_task() == 0
            assert ms.call_args[1]["task_type"] == "coding"
            assert ms.call_args[1]["validate_response"] is not None


VALID_AGENT_OUTPUT = (
    'Done.\n\n```json\n{"summary": "Added utils", "files_changed": ["src/utils.py"]}\n```'
)


class TestCodingResponseValidator:
    def test_valid_output_returns_none(self) -> None:
        assert _coding_response_validator(VALID_AGENT_OUTPUT) is None

    def test_missing_json_returns_retry_prompt(self) -> None:
        result = _coding_response_validator("I made some changes to the code.")
        assert result is not None
        assert "files_changed" in result

    def test_malformed_json_returns_retry_prompt(self) -> None:
        result = _coding_response_validator('```json\n{"bad": true}\n```')
        assert result is not None


class TestParseAgentOutput:
    def test_valid_block(self) -> None:
        out = parse_agent_output(VALID_AGENT_OUTPUT)
        assert out is not None
        assert out.summary == "Added utils"
        assert out.files_changed == ["src/utils.py"]

    def test_no_json_block(self) -> None:
        assert parse_agent_output("No JSON here") is None

    def test_invalid_json_returns_none(self) -> None:
        assert parse_agent_output("```json\nnot json\n```") is None

    def test_missing_fields_returns_none(self) -> None:
        assert parse_agent_output('```json\n{"summary": "x"}\n```') is None


DELETED_FILE_OUTPUT = (
    '```json\n{"summary": "Removed old module",'
    ' "files_changed": ["src/old.py", "src/utils.py"]}\n```'
)
TRAVERSAL_OUTPUT = (
    '```json\n{"summary": "Hack", "files_changed": ["../../etc/passwd", "src/ok.py"]}\n```'
)


class TestBuildCodingResult:
    async def test_deleted_file_staged_via_git_add(self) -> None:
        """Deleted files are staged with `git add --` (no exists() gate)."""
        git_mock = AsyncMock(return_value="")
        with (
            patch(f"{_M}._run_git_simple", git_mock),
            patch(f"{_M}.git_head_sha", AsyncMock(return_value="abc123")),
            patch(f"{_M}.git_diff_staged", AsyncMock(return_value="diff --git ...")),
        ):
            result = await _build_coding_result(Path("/repo"), DELETED_FILE_OUTPUT, AsyncMock())
        data = json.loads(result)
        assert data["summary"] == "Removed old module"
        # Both files staged regardless of whether they exist on disk
        assert git_mock.await_count == 2
        git_mock.assert_any_await(Path("/repo"), "add", "--", "src/old.py")
        git_mock.assert_any_await(Path("/repo"), "add", "--", "src/utils.py")

    async def test_path_traversal_skipped(self) -> None:
        """Files with .. path components are skipped."""
        git_mock = AsyncMock(return_value="")
        with (
            patch(f"{_M}._run_git_simple", git_mock),
            patch(f"{_M}.git_head_sha", AsyncMock(return_value="abc123")),
            patch(f"{_M}.git_diff_staged", AsyncMock(return_value="diff --git ...")),
        ):
            await _build_coding_result(Path("/repo"), TRAVERSAL_OUTPUT, AsyncMock())
        # Only src/ok.py staged; ../../etc/passwd skipped
        git_mock.assert_awaited_once_with(Path("/repo"), "add", "--", "src/ok.py")
