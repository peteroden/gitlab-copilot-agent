"""Factory functions for concurrency primitives (lock, dedup, result store, task queue).

All distributed state now uses Azure Storage (Queue + Blob) for dispatch and results.
Lock uses in-memory implementation (single-controller deployment).
Dedup uses Azure Table Storage when configured, otherwise in-memory.
"""

from __future__ import annotations

from gitlab_copilot_agent.concurrency import (
    DeduplicationStore,
    DistributedLock,
    MemoryDedup,
    MemoryLock,
    MemoryResultStore,
    MemoryTaskQueue,
    ResultStore,
    TaskQueue,
)


def create_lock() -> DistributedLock:
    """Factory: create a DistributedLock (in-memory for single-controller)."""
    return MemoryLock()


def create_dedup(
    *,
    azure_storage_account_url: str | None = None,
    azure_storage_connection_string: str | None = None,
) -> DeduplicationStore:
    """Factory: create a DeduplicationStore.

    Uses TableDedup when Azure Storage is configured, otherwise in-memory.
    """
    if azure_storage_connection_string or azure_storage_account_url:
        from gitlab_copilot_agent.azure_storage import create_table_dedup_store

        return create_table_dedup_store(
            azure_storage_account_url,
            connection_string=azure_storage_connection_string,
        )
    return MemoryDedup()


def create_result_store(
    *,
    azure_storage_account_url: str | None = None,
    azure_storage_connection_string: str | None = None,
    task_blob_container: str = "task-data",
) -> ResultStore:
    """Factory: create a ResultStore.

    Uses BlobResultStore when Azure Storage is configured (connection string
    or account URL), otherwise falls back to in-memory.
    """
    if azure_storage_connection_string or azure_storage_account_url:
        from gitlab_copilot_agent.azure_storage import create_blob_result_store

        return create_blob_result_store(
            azure_storage_account_url,
            task_blob_container,
            connection_string=azure_storage_connection_string,
        )
    return MemoryResultStore()


def create_task_queue(
    *,
    azure_storage_queue_url: str | None = None,
    azure_storage_account_url: str | None = None,
    azure_storage_connection_string: str | None = None,
    task_queue_name: str = "task-queue",
    task_blob_container: str = "task-data",
) -> TaskQueue:
    """Factory: create a TaskQueue.

    Uses AzureStorageTaskQueue when Azure Storage is configured (connection
    string or account URL + queue URL), otherwise falls back to in-memory.
    """
    if azure_storage_connection_string or (azure_storage_queue_url and azure_storage_account_url):
        from gitlab_copilot_agent.azure_storage import (
            create_task_queue as _create_azure_queue,
        )

        return _create_azure_queue(
            azure_storage_queue_url,
            azure_storage_account_url,
            task_queue_name,
            task_blob_container,
            connection_string=azure_storage_connection_string,
        )
    return MemoryTaskQueue()
