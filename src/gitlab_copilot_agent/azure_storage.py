"""Azure Storage Queue + Blob implementations for TaskQueue and ResultStore.

Uses the Claim Check pattern: enqueue uploads params to Blob Storage
and puts a lightweight reference on the queue.  Dequeue fetches the
blob transparently.  Auth is via ``DefaultAzureCredential`` (managed
identity in Azure, CLI locally).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from gitlab_copilot_agent.concurrency import QueueMessage, ResultStore, TaskQueue

if TYPE_CHECKING:
    from azure.data.tables import TableClient
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


def _split_dedup_key(key: str) -> tuple[str, str]:
    """Split a dedup key into PartitionKey and RowKey.

    The first segment before ':' becomes PartitionKey, the rest is RowKey.
    If no colon, PartitionKey is 'default'.
    """
    if ":" in key:
        pk, rk = key.split(":", 1)
        return pk, rk
    return "default", key


class TableDedup:
    """Azure Table Storage-backed dedup store.

    Each key is a row in a 'dedup' table.  PartitionKey is the key prefix
    (e.g., 'note'), RowKey is the remainder.  TTL is enforced at read time
    by checking the entity Timestamp.
    """

    def __init__(self, table_client: TableClient) -> None:
        self._table = table_client

    async def is_seen(self, key: str) -> bool:
        pk, rk = _split_dedup_key(key)
        try:
            entity = await asyncio.to_thread(
                self._table.get_entity,  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                partition_key=pk,
                row_key=rk,
            )
        except Exception as exc:
            exc_str = str(exc)
            if "ResourceNotFound" in exc_str or "Not Found" in exc_str:
                return False
            # Fail closed on auth/permission errors (persistent — won't self-heal).
            # Fail open on transient errors (network, throttling — will resolve on retry).
            if "Authorization" in exc_str or "Forbidden" in exc_str or "Permission" in exc_str:
                log.warning("dedup_is_seen_auth_error_fail_closed", key=key, error=exc_str[:200])
                return True
            log.warning("dedup_is_seen_transient_error", key=key, error=exc_str[:200])
            return False
        raw_ttl = entity.get("ttl_seconds")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        ttl = raw_ttl if isinstance(raw_ttl, int) else 3600
        meta: dict[str, Any] = getattr(entity, "metadata", {})
        ts = meta.get("timestamp")
        if isinstance(ts, datetime):
            age = (datetime.now(tz=UTC) - ts).total_seconds()
            if age > ttl:
                return False
        return True

    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None:
        pk, rk = _split_dedup_key(key)
        entity: dict[str, str | int] = {
            "PartitionKey": pk,
            "RowKey": rk,
            "ttl_seconds": ttl_seconds,
        }
        try:
            await asyncio.to_thread(
                self._table.upsert_entity,  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                entity,
            )
        except Exception:
            log.warning("dedup_mark_seen_failed", key=key, exc_info=True)

    async def aclose(self) -> None:
        """Close the table client."""
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._table.close)


def create_table_dedup_store(
    account_url: str | None,
    table_name: str = "dedup",
    *,
    connection_string: str | None = None,
) -> TableDedup:
    """Create a TableDedup backed by Azure Table Storage."""
    from azure.data.tables import TableServiceClient

    if connection_string:
        service = TableServiceClient.from_connection_string(connection_string)
    elif account_url:
        from azure.identity import DefaultAzureCredential

        table_url = account_url.replace(".blob.", ".table.")
        service = TableServiceClient(endpoint=table_url, credential=DefaultAzureCredential())
    else:
        msg = "Either account_url or connection_string is required"
        raise ValueError(msg)

    with contextlib.suppress(Exception):
        service.create_table_if_not_exists(table_name)  # pyright: ignore[reportUnknownMemberType]

    table_client = service.get_table_client(table_name)
    return TableDedup(table_client)
