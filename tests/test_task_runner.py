import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    _dequeue_task,
    _get_required_env,
    _parse_task_payload,
    _store_result,
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
        with (
            patch(f"{_M}._dequeue_task", AsyncMock(return_value=None)),
            patch(f"{_M}.git_clone", AsyncMock(return_value=fp)),
            patch(f"{_M}.run_copilot_session", AsyncMock(return_value="done")),
            patch(f"{_M}._store_result", AsyncMock()) as store,
            patch(f"{_M}.shutil.rmtree") as rm,
        ):
            assert await run_task() == 0
            rm.assert_called_once_with(fp, ignore_errors=True)
            store.assert_awaited_once()

    async def test_missing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_TASK_TYPE, raising=False)
        with patch(f"{_M}._dequeue_task", AsyncMock(return_value=None)):
            assert await run_task() == 1

    async def test_bad_type(self, task_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_TASK_TYPE, "bad")
        with patch(f"{_M}._dequeue_task", AsyncMock(return_value=None)):
            assert await run_task() == 1

    async def test_url_mismatch(self, task_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_REPO_URL, BAD_HOST)
        with (
            patch(f"{_M}._dequeue_task", AsyncMock(return_value=None)),
            patch(f"{_M}._store_result", AsyncMock()) as store,
        ):
            assert await run_task() == 1
            store.assert_awaited_once()
            stored = json.loads(store.call_args[0][1])
            assert stored["result_type"] == "error"
            assert "does not match" in stored["summary"]

    async def test_coding(self, task_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_TASK_TYPE, "coding")
        coding_json = json.dumps(
            {"result_type": "coding", "summary": "x", "patch": "p", "base_sha": "abc"}
        )
        with (
            patch(f"{_M}._dequeue_task", AsyncMock(return_value=None)),
            patch(f"{_M}.git_clone", AsyncMock(return_value=Path("/tmp/r"))),
            patch(f"{_M}.run_copilot_session", AsyncMock(return_value="x")) as ms,
            patch(f"{_M}._build_coding_result", AsyncMock(return_value=coding_json)),
            patch(f"{_M}._store_result", AsyncMock()),
            patch(f"{_M}.shutil.rmtree"),
            patch("gitlab_copilot_agent.coding_engine.ensure_git_exclude"),
        ):
            assert await run_task() == 0
            assert ms.call_args[1]["task_type"] == "coding"
            assert ms.call_args[1]["validate_response"] is not None

    async def test_failure_writes_error_result(self, task_env: None) -> None:
        with (
            patch(f"{_M}._dequeue_task", AsyncMock(return_value=None)),
            patch(f"{_M}.git_clone", AsyncMock(return_value=Path("/tmp/r"))),
            patch(f"{_M}.run_copilot_session", AsyncMock(side_effect=RuntimeError("boom"))),
            patch(f"{_M}._store_result", AsyncMock()) as store,
            patch(f"{_M}.shutil.rmtree"),
        ):
            assert await run_task() == 1
            store.assert_awaited_once()
            stored = json.loads(store.call_args[0][1])
            assert stored["result_type"] == "error"
            assert stored["error"] is True
            assert "boom" in stored["summary"]


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


_STATE_MOD = "gitlab_copilot_agent.state"


class TestStoreResult:
    """Tests for _store_result function."""

    async def test_skips_when_no_settings(self) -> None:
        """Returns immediately when no settings are provided."""
        await _store_result("task-1", '{"result": "ok"}')  # Should not raise

    async def test_stores_via_azure_storage(self) -> None:
        """Stores result in Azure Storage when settings are provided."""
        mock_store = MagicMock()
        mock_store.set = AsyncMock()
        mock_store.aclose = AsyncMock()
        with patch(f"{_STATE_MOD}.create_result_store", return_value=mock_store):
            from gitlab_copilot_agent.config import TaskRunnerSettings

            settings = TaskRunnerSettings(
                gitlab_url=GITLAB_URL,
                gitlab_token="t",
                github_token="g",
                azure_storage_connection_string="conn",
            )
            await _store_result("task-1", '{"result": "ok"}', settings)
        mock_store.set.assert_awaited_once_with("task-1", '{"result": "ok"}')
        mock_store.aclose.assert_awaited_once()

    async def test_closes_store_on_error(self) -> None:
        """Store is closed even if set() raises."""
        mock_store = MagicMock()
        mock_store.set = AsyncMock(side_effect=RuntimeError("boom"))
        mock_store.aclose = AsyncMock()
        with (
            patch(f"{_STATE_MOD}.create_result_store", return_value=mock_store),
            pytest.raises(RuntimeError, match="boom"),
        ):
            from gitlab_copilot_agent.config import TaskRunnerSettings

            settings = TaskRunnerSettings(
                gitlab_url=GITLAB_URL,
                gitlab_token="t",
                github_token="g",
                azure_storage_connection_string="conn",
            )
            await _store_result("task-1", '{"result": "ok"}', settings)
        mock_store.aclose.assert_awaited_once()


class TestDequeueTask:
    """Tests for _dequeue_task function."""

    async def test_returns_none_when_settings_invalid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns None when TaskRunnerSettings can't be created."""
        monkeypatch.delenv("GITLAB_URL", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
        result = await _dequeue_task()
        assert result is None

    async def test_returns_none_when_no_azure_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns None when no Azure Storage is configured."""
        monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
        monkeypatch.setenv("GITLAB_TOKEN", "t")
        monkeypatch.setenv("GITHUB_TOKEN", "g")
        monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
        monkeypatch.delenv("AZURE_STORAGE_QUEUE_URL", raising=False)
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
        result = await _dequeue_task()
        assert result is None

    async def test_returns_none_when_queue_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns None and closes queue when no messages."""
        monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
        monkeypatch.setenv("GITLAB_TOKEN", "t")
        monkeypatch.setenv("GITHUB_TOKEN", "g")
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "conn")
        mock_queue = MagicMock()
        mock_queue.dequeue = AsyncMock(return_value=None)
        mock_queue.aclose = AsyncMock()
        with patch(f"{_STATE_MOD}.create_task_queue", return_value=mock_queue):
            result = await _dequeue_task()
        assert result is None
        mock_queue.aclose.assert_awaited_once()

    async def test_returns_params_and_queue_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns parsed params, message, and queue on successful dequeue."""
        monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
        monkeypatch.setenv("GITLAB_TOKEN", "t")
        monkeypatch.setenv("GITHUB_TOKEN", "g")
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "conn")

        from gitlab_copilot_agent.concurrency import QueueMessage

        fake_msg = QueueMessage(
            message_id="m1",
            receipt="r1",
            task_id="task-1",
            payload=json.dumps({"task_type": "review", "task_id": "task-1"}),
            dequeue_count=1,
        )
        mock_queue = MagicMock()
        mock_queue.dequeue = AsyncMock(return_value=fake_msg)
        with patch(f"{_STATE_MOD}.create_task_queue", return_value=mock_queue):
            result = await _dequeue_task()
        assert result is not None
        params, msg, queue = result
        assert params == {"task_type": "review", "task_id": "task-1"}
        assert msg.message_id == "m1"
        assert queue is mock_queue
