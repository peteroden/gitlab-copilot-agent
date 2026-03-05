"""Tests for factory functions in state.py — Memory fallbacks and Azure Storage dispatch."""

from __future__ import annotations

from unittest.mock import patch

from gitlab_copilot_agent.azure_storage import AzureStorageTaskQueue, BlobResultStore
from gitlab_copilot_agent.concurrency import (
    MemoryDedup,
    MemoryLock,
    MemoryResultStore,
    MemoryTaskQueue,
)
from gitlab_copilot_agent.state import (
    create_dedup,
    create_lock,
    create_result_store,
    create_task_queue,
)

CONN_STR = "DefaultEndpointsProtocol=http;AccountName=test;AccountKey=dGVzdA=="


def test_create_lock_returns_memory_lock() -> None:
    assert isinstance(create_lock(), MemoryLock)


def test_create_dedup_returns_memory_dedup() -> None:
    assert isinstance(create_dedup(), MemoryDedup)


def test_create_result_store_returns_memory_by_default() -> None:
    store = create_result_store()
    assert isinstance(store, MemoryResultStore)


def test_create_task_queue_returns_memory_by_default() -> None:
    queue = create_task_queue()
    assert isinstance(queue, MemoryTaskQueue)


def test_create_result_store_delegates_to_azure_with_conn_string() -> None:
    with patch("azure.storage.blob.aio.ContainerClient.from_connection_string"):
        store = create_result_store(azure_storage_connection_string=CONN_STR)
    assert isinstance(store, BlobResultStore)


def test_create_task_queue_delegates_to_azure_with_conn_string() -> None:
    with (
        patch("azure.storage.queue.aio.QueueClient.from_connection_string"),
        patch("azure.storage.blob.aio.ContainerClient.from_connection_string"),
    ):
        queue = create_task_queue(azure_storage_connection_string=CONN_STR)
    assert isinstance(queue, AzureStorageTaskQueue)
