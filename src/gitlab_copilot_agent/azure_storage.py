"""Azure Storage Queue + Blob implementations for TaskQueue and ResultStore.

Uses the Claim Check pattern: enqueue uploads params to Blob Storage
and puts a lightweight reference on the queue.  Dequeue fetches the
blob transparently.  Auth is via ``DefaultAzureCredential`` (managed
identity in Azure, CLI locally).
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import structlog

from gitlab_copilot_agent.concurrency import QueueMessage, ResultStore, TaskQueue

if TYPE_CHECKING:
    from azure.identity.aio import DefaultAzureCredential
    from azure.storage.blob.aio import ContainerClient
    from azure.storage.queue.aio import QueueClient

log = structlog.get_logger()

_PARAMS_PREFIX = "params/"
_RESULTS_PREFIX = "results/"
_MAX_DEQUEUE_COUNT = 5


class AzureStorageTaskQueue:
    """Azure Storage Queue + Blob task queue (Claim Check pattern).

    ``enqueue``: upload params blob → put queue message referencing it.
    ``dequeue``: receive queue message → download params blob → return full payload.
    ``complete``: delete queue message (blob cleaned by lifecycle policy).
    """

    def __init__(
        self,
        queue_client: QueueClient,
        blob_client: ContainerClient,
        credential: DefaultAzureCredential | None = None,
    ) -> None:
        self._queue = queue_client
        self._blob = blob_client
        self._credential = credential

    async def enqueue(self, task_id: str, payload: str) -> None:
        blob_name = f"{_PARAMS_PREFIX}{task_id}.json"
        blob = self._blob.get_blob_client(blob_name)
        await blob.upload_blob(payload, overwrite=True)
        try:
            message_body = json.dumps({"task_id": task_id, "blob": blob_name})
            await self._queue.send_message(message_body)
        except Exception:
            # Compensating delete — avoid orphaned blob
            with contextlib.suppress(Exception):
                await blob.delete_blob()
            raise
        log.info("task_enqueued", task_id=task_id, blob=blob_name)

    async def dequeue(self, visibility_timeout: int = 300) -> QueueMessage | None:
        messages = self._queue.receive_messages(
            max_messages=1, visibility_timeout=visibility_timeout
        )
        msg: Any = None
        async for m in messages:
            msg = m
            break
        if msg is None:
            return None

        try:
            body = json.loads(msg.content)
            task_id: str = body["task_id"]
            blob_name: str = body["blob"]
            blob = self._blob.get_blob_client(blob_name)
            download = await blob.download_blob()
            payload = (await download.readall()).decode()
        except Exception:
            dequeue_count: int = msg.dequeue_count or 1
            if dequeue_count >= _MAX_DEQUEUE_COUNT:
                log.error("poison_message_deleted", message_id=msg.id, attempts=dequeue_count)
                with contextlib.suppress(Exception):
                    await self._queue.delete_message(msg.id, msg.pop_receipt)
            else:
                log.warning("dequeue_parse_failed", message_id=msg.id, attempts=dequeue_count)
            return None

        return QueueMessage(
            message_id=str(msg.id),
            receipt=str(msg.pop_receipt),
            task_id=task_id,
            payload=payload,
            dequeue_count=msg.dequeue_count or 1,
        )

    async def complete(self, message: QueueMessage) -> None:
        await self._queue.delete_message(message.message_id, message.receipt)
        log.info("task_completed", task_id=message.task_id, message_id=message.message_id)

    async def upload_blob(self, name: str, data: bytes) -> None:
        blob = self._blob.get_blob_client(name)
        await blob.upload_blob(data, overwrite=True)

    async def download_blob(self, name: str) -> bytes:
        blob = self._blob.get_blob_client(name)
        download = await blob.download_blob()
        return await download.readall()

    async def aclose(self) -> None:
        await self._queue.close()
        await self._blob.close()
        if self._credential is not None:
            await self._credential.close()


class BlobResultStore:
    """Azure Blob Storage result store implementing ``ResultStore``.

    Results are stored as blobs under ``results/{key}.json``.
    TTL is handled by the container lifecycle policy, not per-blob.
    """

    def __init__(
        self,
        blob_client: ContainerClient,
        credential: DefaultAzureCredential | None = None,
    ) -> None:
        self._blob = blob_client
        self._credential = credential

    async def get(self, key: str) -> str | None:
        blob = self._blob.get_blob_client(f"{_RESULTS_PREFIX}{key}.json")
        try:
            download = await blob.download_blob()
            content: bytes = await download.readall()
            return content.decode()
        except Exception:
            return None

    async def set(self, key: str, value: str, ttl: int = 3600) -> None:
        blob = self._blob.get_blob_client(f"{_RESULTS_PREFIX}{key}.json")
        await blob.upload_blob(value, overwrite=True)

    async def aclose(self) -> None:
        await self._blob.close()
        if self._credential is not None:
            await self._credential.close()


def create_task_queue(
    queue_url: str | None,
    account_url: str | None,
    queue_name: str,
    container_name: str,
    connection_string: str | None = None,
) -> TaskQueue:
    """Create an AzureStorageTaskQueue with connection string or DefaultAzureCredential."""
    from azure.storage.blob.aio import ContainerClient
    from azure.storage.queue.aio import QueueClient

    credential = None
    if connection_string:
        queue_client = QueueClient.from_connection_string(connection_string, queue_name=queue_name)
        blob_client = ContainerClient.from_connection_string(
            connection_string, container_name=container_name
        )
    else:
        from azure.identity.aio import DefaultAzureCredential

        if not queue_url or not account_url:
            msg = "queue_url and account_url required when no connection_string"
            raise ValueError(msg)
        credential = DefaultAzureCredential()
        queue_client = QueueClient(queue_url, queue_name=queue_name, credential=credential)
        blob_client = ContainerClient(
            account_url, container_name=container_name, credential=credential
        )
    return AzureStorageTaskQueue(queue_client, blob_client, credential)


def create_blob_result_store(
    account_url: str | None,
    container_name: str,
    connection_string: str | None = None,
) -> ResultStore:
    """Create a BlobResultStore with connection string or DefaultAzureCredential."""
    from azure.storage.blob.aio import ContainerClient

    credential = None
    if connection_string:
        blob_client = ContainerClient.from_connection_string(
            connection_string, container_name=container_name
        )
    else:
        from azure.identity.aio import DefaultAzureCredential

        if not account_url:
            msg = "account_url required when no connection_string"
            raise ValueError(msg)
        credential = DefaultAzureCredential()
        blob_client = ContainerClient(
            account_url, container_name=container_name, credential=credential
        )
    return BlobResultStore(blob_client, credential)
