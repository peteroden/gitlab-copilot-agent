import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gitlab_copilot_agent.coding_engine import parse_agent_output
from gitlab_copilot_agent.task_runner import (
    ENV_TASK_ID,
    ENV_TASK_PAYLOAD,
    ENV_TASK_TYPE,
    QueueTaskPayload,
    _build_coding_result,
    _coding_response_validator,
    _dequeue_task,
    _get_required_env,
    _list_changed_paths,
    _parse_task_payload,
    _store_result,
    run_task,
)

TASK_ID = "task-001"
PAYLOAD = json.dumps({"prompt": "Review this"})
PLUGIN_SPEC = "copilot-plugin-a"
_M = "gitlab_copilot_agent.task_runner"


@pytest.fixture()
def task_env(env_vars: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set task-runner env vars on top of the base env_vars fixture."""
    monkeypatch.setenv(ENV_TASK_TYPE, "review")
    monkeypatch.setenv(ENV_TASK_ID, TASK_ID)
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


REPO_BLOB_KEY = "repos/task-001.tar.gz"
QUEUE_PAYLOAD = QueueTaskPayload(
    task_type="review",
    task_id=TASK_ID,
    repo_blob_key=REPO_BLOB_KEY,
    system_prompt="",
    user_prompt="Review this",
)


def _make_queue_result(
    payload: QueueTaskPayload | None = None,
) -> tuple[QueueTaskPayload, MagicMock, MagicMock]:
    """Build a (payload, queue_msg, task_queue) tuple for _dequeue_task mocks."""
    from gitlab_copilot_agent.concurrency import QueueMessage

    p = payload or QUEUE_PAYLOAD
    msg = QueueMessage(
        message_id="m1",
        receipt="r1",
        task_id=p.task_id,
        payload=p.model_dump_json(),
        dequeue_count=1,
    )
    queue = MagicMock()
    queue.complete = AsyncMock()
    queue.download_blob = AsyncMock(return_value=b"fake-tarball")
    queue.aclose = AsyncMock()
    return p, msg, queue


class TestRunTask:
    async def test_ok(self, task_env: None) -> None:
        fp = Path("/tmp/fake")
        qr = _make_queue_result()
        with (
            patch(f"{_M}._dequeue_task", AsyncMock(return_value=qr)),
            patch(f"{_M}.extract_repo_tarball", AsyncMock(return_value=fp)),
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

    async def test_missing_blob_key_returns_error(self, task_env: None) -> None:
        """Review/coding tasks without repo_blob_key return error and close queue."""
        payload = QUEUE_PAYLOAD.model_copy(update={"repo_blob_key": None})
        _, msg, queue = _make_queue_result(payload)
        qr = (payload, msg, queue)
        with patch(f"{_M}._dequeue_task", AsyncMock(return_value=qr)):
            assert await run_task() == 1
        queue.aclose.assert_awaited_once()

    async def test_env_path_without_queue_returns_error(self, task_env: None) -> None:
        """Env-var path (no queue) cannot run review/coding tasks."""
        with patch(f"{_M}._dequeue_task", AsyncMock(return_value=None)):
            assert await run_task() == 1

    async def test_invalid_blob_key_prefix_returns_error(self, task_env: None) -> None:
        """Blob keys must start with 'repos/' prefix; queue is closed."""
        payload = QUEUE_PAYLOAD.model_copy(update={"repo_blob_key": "evil/path.tar.gz"})
        _, msg, queue = _make_queue_result(payload)
        qr = (payload, msg, queue)
        with patch(f"{_M}._dequeue_task", AsyncMock(return_value=qr)):
            assert await run_task() == 1
        queue.aclose.assert_awaited_once()

    async def test_coding(self, task_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        coding_json = json.dumps(
            {"result_type": "coding", "summary": "x", "patch": "p", "base_sha": "abc"}
        )
        payload = QUEUE_PAYLOAD.model_copy(update={"task_type": "coding"})
        qr = _make_queue_result(payload)
        with (
            patch(f"{_M}._dequeue_task", AsyncMock(return_value=qr)),
            patch(f"{_M}.extract_repo_tarball", AsyncMock(return_value=Path("/tmp/r"))),
            patch(f"{_M}.git_head_sha", AsyncMock(return_value="abc123")),
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
        copilot_error = "Copilot session timed out after 30s"
        qr = _make_queue_result()
        with (
            patch(f"{_M}._dequeue_task", AsyncMock(return_value=qr)),
            patch(f"{_M}.extract_repo_tarball", AsyncMock(return_value=Path("/tmp/r"))),
            patch(
                f"{_M}.run_copilot_session",
                AsyncMock(side_effect=RuntimeError(copilot_error)),
            ),
            patch(f"{_M}._store_result", AsyncMock()) as store,
            patch(f"{_M}.shutil.rmtree"),
        ):
            assert await run_task() == 1
            store.assert_awaited_once()
            stored = json.loads(store.call_args[0][1])
            assert stored["result_type"] == "error"
            assert stored["error"] is True
            assert copilot_error in stored["summary"]


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
            result = await _build_coding_result(
                Path("/repo"), DELETED_FILE_OUTPUT, AsyncMock(), "abc123"
            )
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
            await _build_coding_result(Path("/repo"), TRAVERSAL_OUTPUT, AsyncMock(), "abc123")
        # Only src/ok.py staged; ../../etc/passwd skipped
        git_mock.assert_awaited_once_with(Path("/repo"), "add", "--", "src/ok.py")

    async def test_agent_committed_fallback(self) -> None:
        """When staged diff is empty but HEAD moved, diff against pre-session SHA."""
        git_mock = AsyncMock(return_value="diff --git a/src/utils.py ...")
        with (
            patch(f"{_M}._run_git_simple", git_mock),
            patch(f"{_M}.git_head_sha", AsyncMock(return_value="def456")),
            patch(f"{_M}.git_diff_staged", AsyncMock(return_value="")),
        ):
            result = await _build_coding_result(
                Path("/repo"), DELETED_FILE_OUTPUT, AsyncMock(), "abc123"
            )
        data = json.loads(result)
        assert data["patch"] == "diff --git a/src/utils.py ...\n"
        assert data["base_sha"] == "abc123"
        # Verify fallback diff was called with pre-session SHA
        git_mock.assert_any_await(Path("/repo"), "diff", "abc123", "HEAD", "--binary")

    async def test_invalid_agent_output_falls_back_to_changed_paths(self) -> None:
        """Malformed or missing files_changed falls back to staging changed paths only."""
        git_mock = AsyncMock(return_value="")
        with (
            patch(f"{_M}._run_git_simple", git_mock),
            patch(
                f"{_M}._list_changed_paths",
                AsyncMock(return_value=["src/app.py", "tests/test_app.py"]),
            ),
            patch(f"{_M}.git_head_sha", AsyncMock(return_value="abc123")),
            patch(f"{_M}.git_diff_staged", AsyncMock(return_value="diff --git ...")),
        ):
            result = await _build_coding_result(
                Path("/repo"),
                "I updated the FastAPI app and tests.",
                AsyncMock(),
                "abc123",
            )
        data = json.loads(result)
        assert data["summary"] == "I updated the FastAPI app and tests."
        assert data["patch"] == "diff --git ..."
        assert git_mock.await_count == 2
        git_mock.assert_any_await(Path("/repo"), "add", "-A", "--", "src/app.py")
        git_mock.assert_any_await(Path("/repo"), "add", "-A", "--", "tests/test_app.py")

    async def test_list_changed_paths_combines_tracked_staged_and_untracked(self) -> None:
        """Fallback path discovery includes unstaged, staged, and untracked paths."""
        git_mock = AsyncMock(
            side_effect=[
                "src/app.py\nsrc/old.py\n",
                "src/staged.py\n",
                "tests/test_app.py\n",
            ]
        )
        with patch(f"{_M}._run_git_simple", git_mock):
            paths = await _list_changed_paths(Path("/repo"))
        assert paths == ["src/app.py", "src/old.py", "src/staged.py", "tests/test_app.py"]

    async def test_no_changes_returns_empty_patch(self) -> None:
        """No files_changed and no repo changes returns summary with empty patch."""
        with patch(f"{_M}._list_changed_paths", AsyncMock(return_value=[])):
            result = await _build_coding_result(
                Path("/repo"),
                "I updated the FastAPI app and tests.",
                AsyncMock(),
                "abc123",
            )
        import json

        parsed = json.loads(result)
        assert parsed["patch"] == ""
        assert parsed["result_type"] == "coding"

    async def test_no_changes_logs_and_preserves_summary(self) -> None:
        """No changes logs info and preserves raw LLM response as summary."""
        agent_response = "I tried but could not make changes to the repository."
        mock_log = AsyncMock()
        with patch(f"{_M}._list_changed_paths", AsyncMock(return_value=[])):
            result = await _build_coding_result(
                Path("/repo"),
                agent_response,
                mock_log,
                "abc123",
            )
        import json

        parsed = json.loads(result)
        assert parsed["summary"] == agent_response
        mock_log.awarning.assert_any_await("agent_output_parse_failed", raw_excerpt=agent_response)
        mock_log.ainfo.assert_any_await("no_code_changes")


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
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("COPILOT_PROVIDER_TYPE", raising=False)
        monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
        result = await _dequeue_task()
        assert result is None

    async def test_returns_none_when_no_azure_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns None when no Azure Storage is configured."""
        monkeypatch.setenv("GITHUB_TOKEN", "g")
        monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
        monkeypatch.delenv("AZURE_STORAGE_QUEUE_URL", raising=False)
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
        result = await _dequeue_task()
        assert result is None

    async def test_returns_none_when_queue_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns None and closes queue when no messages."""
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
        """Returns parsed payload, message, and queue on successful dequeue."""
        monkeypatch.setenv("GITHUB_TOKEN", "g")
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "conn")

        from gitlab_copilot_agent.concurrency import QueueMessage

        payload_data = {"task_type": "review", "task_id": "task-1", "user_prompt": "Review this"}
        fake_msg = QueueMessage(
            message_id="m1",
            receipt="r1",
            task_id="task-1",
            payload=json.dumps(payload_data),
            dequeue_count=1,
        )
        mock_queue = MagicMock()
        mock_queue.dequeue = AsyncMock(return_value=fake_msg)
        with patch(f"{_STATE_MOD}.create_task_queue", return_value=mock_queue):
            result = await _dequeue_task()
        assert result is not None
        payload, msg, queue = result
        assert isinstance(payload, QueueTaskPayload)
        assert payload.task_type == "review"
        assert payload.task_id == "task-1"
        assert msg.message_id == "m1"
        assert queue is mock_queue


class TestPluginThreading:
    async def test_plugins_passed_from_queue_to_session(self, task_env: None) -> None:
        """Plugins from queue payload are passed to run_copilot_session."""
        payload = QUEUE_PAYLOAD.model_copy(update={"plugins": [PLUGIN_SPEC]})
        qr = _make_queue_result(payload)
        with (
            patch(f"{_M}._dequeue_task", AsyncMock(return_value=qr)),
            patch(f"{_M}.extract_repo_tarball", AsyncMock(return_value=Path("/tmp/r"))),
            patch(f"{_M}.run_copilot_session", AsyncMock(return_value="done")) as ms,
            patch(f"{_M}._store_result", AsyncMock()),
            patch(f"{_M}.shutil.rmtree"),
        ):
            assert await run_task() == 0
            assert ms.call_args[1]["plugins"] == [PLUGIN_SPEC]

    async def test_no_plugins_in_payload(self, task_env: None) -> None:
        """Missing plugins field in queue payload passes None to session."""
        qr = _make_queue_result()
        with (
            patch(f"{_M}._dequeue_task", AsyncMock(return_value=qr)),
            patch(f"{_M}.extract_repo_tarball", AsyncMock(return_value=Path("/tmp/r"))),
            patch(f"{_M}.run_copilot_session", AsyncMock(return_value="done")) as ms,
            patch(f"{_M}._store_result", AsyncMock()),
            patch(f"{_M}.shutil.rmtree"),
        ):
            assert await run_task() == 0
            assert ms.call_args[1]["plugins"] is None
