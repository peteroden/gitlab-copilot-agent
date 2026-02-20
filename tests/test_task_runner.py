import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gitlab_copilot_agent.task_runner import (
    ENV_BRANCH,
    ENV_REPO_URL,
    ENV_TASK_ID,
    ENV_TASK_PAYLOAD,
    ENV_TASK_TYPE,
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
        ):
            assert await run_task() == 0
            assert ms.call_args[1]["task_type"] == "coding"
