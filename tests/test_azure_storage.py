"""Tests for Azure Storage Queue + Blob implementations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gitlab_copilot_agent.azure_storage import AzureStorageTaskQueue, BlobResultStore
from gitlab_copilot_agent.concurrency import QueueMessage

TASK_ID = "task-abc-123"
PAYLOAD = json.dumps({"project_id": 42, "mr_iid": 7})
BLOB_NAME = f"params/{TASK_ID}.json"
RESULT_KEY = "result-xyz"
RESULT_VALUE = json.dumps({"status": "success", "diff": "@@..."})


@dataclass
class FakeQueueMessage:
    """Mimics azure.storage.queue.QueueMessage attributes."""

    id: str
    pop_receipt: str
    content: str
    dequeue_count: int


async def _async_iter_one(msg: FakeQueueMessage) -> Any:
    yield msg


async def _async_iter_empty() -> Any:
    return
    yield  # noqa: B027


def _make_blob_download(data: str) -> AsyncMock:
    download = AsyncMock()
    download.readall = AsyncMock(return_value=data.encode())
    return download


def _make_clients() -> tuple[MagicMock, MagicMock]:
    """Mock queue_client + blob_client (MagicMock for sync factory methods)."""
    queue_client = MagicMock()
    queue_client.send_message = AsyncMock()
    queue_client.delete_message = AsyncMock()
    queue_client.close = AsyncMock()
    blob_client = MagicMock()
    blob_client.close = AsyncMock()
    return queue_client, blob_client


class TestAzureStorageTaskQueue:
    async def test_enqueue_uploads_blob_and_sends_message(self) -> None:
        queue_client, blob_client = _make_clients()
        blob_ref = AsyncMock()
        blob_client.get_blob_client.return_value = blob_ref

        tq = AzureStorageTaskQueue(queue_client, blob_client)
        await tq.enqueue(TASK_ID, PAYLOAD)

        blob_client.get_blob_client.assert_called_once_with(BLOB_NAME)
        blob_ref.upload_blob.assert_awaited_once_with(PAYLOAD, overwrite=True)
        queue_client.send_message.assert_awaited_once()
        sent_body = json.loads(queue_client.send_message.call_args[0][0])
        assert sent_body == {"task_id": TASK_ID, "blob": BLOB_NAME}

    async def test_dequeue_returns_message_with_payload(self) -> None:
        queue_client, blob_client = _make_clients()
        fake_msg = FakeQueueMessage(
            id="msg-1",
            pop_receipt="receipt-1",
            content=json.dumps({"task_id": TASK_ID, "blob": BLOB_NAME}),
            dequeue_count=1,
        )
        queue_client.receive_messages.return_value = _async_iter_one(fake_msg)

        blob_ref = AsyncMock()
        blob_ref.download_blob = AsyncMock(return_value=_make_blob_download(PAYLOAD))
        blob_client.get_blob_client.return_value = blob_ref

        tq = AzureStorageTaskQueue(queue_client, blob_client)
        result = await tq.dequeue(visibility_timeout=60)

        assert result is not None
        assert result.task_id == TASK_ID
        assert result.payload == PAYLOAD
        assert result.message_id == "msg-1"
        assert result.receipt == "receipt-1"
        assert result.dequeue_count == 1

    async def test_dequeue_returns_none_when_empty(self) -> None:
        queue_client, blob_client = _make_clients()
        queue_client.receive_messages.return_value = _async_iter_empty()

        tq = AzureStorageTaskQueue(queue_client, blob_client)
        result = await tq.dequeue()

        assert result is None

    async def test_complete_deletes_message(self) -> None:
        queue_client, blob_client = _make_clients()
        msg = QueueMessage(
            message_id="msg-1",
            receipt="receipt-1",
            task_id=TASK_ID,
            payload=PAYLOAD,
            dequeue_count=1,
        )

        tq = AzureStorageTaskQueue(queue_client, blob_client)
        await tq.complete(msg)

        queue_client.delete_message.assert_awaited_once_with("msg-1", "receipt-1")

    async def test_aclose_closes_both_clients_and_credential(self) -> None:
        queue_client, blob_client = _make_clients()
        credential = AsyncMock()

        tq = AzureStorageTaskQueue(queue_client, blob_client, credential)
        await tq.aclose()

        queue_client.close.assert_awaited_once()
        blob_client.close.assert_awaited_once()
        credential.close.assert_awaited_once()

    async def test_enqueue_cleans_blob_on_queue_failure(self) -> None:
        queue_client, blob_client = _make_clients()
        blob_ref = AsyncMock()
        blob_client.get_blob_client.return_value = blob_ref
        queue_client.send_message = AsyncMock(side_effect=RuntimeError("queue down"))

        tq = AzureStorageTaskQueue(queue_client, blob_client)
        with pytest.raises(RuntimeError, match="queue down"):
            await tq.enqueue(TASK_ID, PAYLOAD)

        blob_ref.delete_blob.assert_awaited_once()

    async def test_dequeue_handles_malformed_message(self) -> None:
        queue_client, blob_client = _make_clients()
        fake_msg = FakeQueueMessage(
            id="msg-bad", pop_receipt="r", content="not-json", dequeue_count=1
        )
        queue_client.receive_messages.return_value = _async_iter_one(fake_msg)

        tq = AzureStorageTaskQueue(queue_client, blob_client)
        result = await tq.dequeue()

        assert result is None
        queue_client.delete_message.assert_not_awaited()

    async def test_dequeue_deletes_poison_message(self) -> None:
        queue_client, blob_client = _make_clients()
        fake_msg = FakeQueueMessage(
            id="msg-poison", pop_receipt="r", content="bad", dequeue_count=5
        )
        queue_client.receive_messages.return_value = _async_iter_one(fake_msg)

        tq = AzureStorageTaskQueue(queue_client, blob_client)
        result = await tq.dequeue()

        assert result is None
        queue_client.delete_message.assert_awaited_once_with("msg-poison", "r")


class TestBlobResultStore:
    async def test_set_uploads_blob(self) -> None:
        _, blob_client = _make_clients()
        blob_ref = AsyncMock()
        blob_client.get_blob_client.return_value = blob_ref

        store = BlobResultStore(blob_client)
        await store.set(RESULT_KEY, RESULT_VALUE)

        blob_client.get_blob_client.assert_called_once_with(f"results/{RESULT_KEY}.json")
        blob_ref.upload_blob.assert_awaited_once_with(RESULT_VALUE, overwrite=True)

    async def test_get_downloads_blob(self) -> None:
        _, blob_client = _make_clients()
        blob_ref = AsyncMock()
        blob_ref.download_blob = AsyncMock(return_value=_make_blob_download(RESULT_VALUE))
        blob_client.get_blob_client.return_value = blob_ref

        store = BlobResultStore(blob_client)
        result = await store.get(RESULT_KEY)

        assert result == RESULT_VALUE

    async def test_get_returns_none_when_missing(self) -> None:
        _, blob_client = _make_clients()
        blob_ref = AsyncMock()
        blob_ref.download_blob = AsyncMock(side_effect=Exception("BlobNotFound"))
        blob_client.get_blob_client.return_value = blob_ref

        store = BlobResultStore(blob_client)
        result = await store.get("nonexistent")

        assert result is None

    async def test_aclose_closes_client_and_credential(self) -> None:
        _, blob_client = _make_clients()
        credential = AsyncMock()
        store = BlobResultStore(blob_client, credential)
        await store.aclose()
        blob_client.close.assert_awaited_once()
        credential.close.assert_awaited_once()


# ── Factory function tests ──────────────────────────────────────────


CONN_STR = "DefaultEndpointsProtocol=http;AccountName=test;AccountKey=dGVzdA==;QueueEndpoint=http://q;BlobEndpoint=http://b"
QUEUE_URL = "https://myaccount.queue.core.windows.net"
ACCOUNT_URL = "https://myaccount.blob.core.windows.net"


class TestCreateTaskQueue:
    """Tests for create_task_queue factory."""

    def test_with_connection_string(self) -> None:
        from gitlab_copilot_agent.azure_storage import create_task_queue

        with (
            patch("azure.storage.queue.aio.QueueClient.from_connection_string") as q_factory,
            patch("azure.storage.blob.aio.ContainerClient.from_connection_string") as b_factory,
        ):
            result = create_task_queue(None, None, "q", "c", connection_string=CONN_STR)
        assert isinstance(result, AzureStorageTaskQueue)
        q_factory.assert_called_once_with(CONN_STR, queue_name="q")
        b_factory.assert_called_once_with(CONN_STR, container_name="c")

    def test_with_account_urls_uses_credential(self) -> None:
        from gitlab_copilot_agent.azure_storage import create_task_queue

        with patch("azure.identity.aio.DefaultAzureCredential") as cred_cls:
            result = create_task_queue(QUEUE_URL, ACCOUNT_URL, "q", "c")
        assert isinstance(result, AzureStorageTaskQueue)
        cred_cls.assert_called_once()

    def test_raises_without_urls_or_connection_string(self) -> None:
        from gitlab_copilot_agent.azure_storage import create_task_queue

        with pytest.raises(ValueError, match="queue_url and account_url required"):
            create_task_queue(None, None, "q", "c")


class TestCreateBlobResultStore:
    """Tests for create_blob_result_store factory."""

    def test_with_connection_string(self) -> None:
        from gitlab_copilot_agent.azure_storage import create_blob_result_store

        with patch("azure.storage.blob.aio.ContainerClient.from_connection_string") as b_factory:
            result = create_blob_result_store(None, "c", connection_string=CONN_STR)
        assert isinstance(result, BlobResultStore)
        b_factory.assert_called_once_with(CONN_STR, container_name="c")

    def test_with_account_url_uses_credential(self) -> None:
        from gitlab_copilot_agent.azure_storage import create_blob_result_store

        with patch("azure.identity.aio.DefaultAzureCredential") as cred_cls:
            result = create_blob_result_store(ACCOUNT_URL, "c")
        assert isinstance(result, BlobResultStore)
        cred_cls.assert_called_once()

    def test_raises_without_account_url_or_connection_string(self) -> None:
        from gitlab_copilot_agent.azure_storage import create_blob_result_store

        with pytest.raises(ValueError, match="account_url required"):
            create_blob_result_store(None, "c")
