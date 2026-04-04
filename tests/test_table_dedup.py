"""Tests for TableDedup Azure Table Storage-backed deduplication store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gitlab_copilot_agent.azure_storage import (
    TableDedup,
    _split_dedup_key,
    create_table_dedup_store,
)

# -- Constants --

DEDUP_KEY = "note:42:7:501"
PARTITION_KEY = "note"
ROW_KEY = "42:7:501"
DEFAULT_TTL = 3600
CONN_STR = "DefaultEndpointsProtocol=http;AccountName=test;AccountKey=dGVzdA=="
ACCOUNT_URL = "https://myaccount.blob.core.windows.net"


# -- Helpers --


class FakeEntity(dict[str, Any]):
    """Mimics azure.data.tables.TableEntity with metadata."""

    def __init__(self, data: dict[str, Any], timestamp: datetime) -> None:
        super().__init__(data)
        self.metadata: dict[str, Any] = {"timestamp": timestamp}


def _make_entity(
    ttl: int = DEFAULT_TTL,
    timestamp: datetime | None = None,
) -> FakeEntity:
    """Create a FakeEntity with sensible defaults."""
    ts = timestamp or datetime.now(tz=UTC)
    return FakeEntity({"ttl_seconds": ttl}, ts)


def _make_table_client() -> MagicMock:
    return MagicMock()


# -- _split_dedup_key tests --


class TestSplitDedupKey:
    def test_splits_on_first_colon(self) -> None:
        assert _split_dedup_key("note:42:7:501") == ("note", "42:7:501")

    def test_single_segment_uses_default_partition(self) -> None:
        assert _split_dedup_key("abc123") == ("default", "abc123")

    def test_colon_at_end(self) -> None:
        assert _split_dedup_key("review:") == ("review", "")

    def test_multiple_colons_only_splits_first(self) -> None:
        assert _split_dedup_key("a:b:c:d") == ("a", "b:c:d")


# -- TableDedup.is_seen tests --


class TestTableDedupIsSeen:
    async def test_returns_false_when_entity_missing(self) -> None:
        table = _make_table_client()
        table.get_entity.side_effect = Exception("ResourceNotFoundError")
        store = TableDedup(table)

        assert await store.is_seen(DEDUP_KEY) is False

    async def test_returns_true_when_entity_exists_within_ttl(self) -> None:
        table = _make_table_client()
        table.get_entity.return_value = _make_entity(ttl=DEFAULT_TTL)
        store = TableDedup(table)

        assert await store.is_seen(DEDUP_KEY) is True
        table.get_entity.assert_called_once_with(partition_key=PARTITION_KEY, row_key=ROW_KEY)

    async def test_returns_false_when_ttl_expired(self) -> None:
        old_ts = datetime.now(tz=UTC) - timedelta(seconds=7200)
        table = _make_table_client()
        table.get_entity.return_value = _make_entity(ttl=DEFAULT_TTL, timestamp=old_ts)
        store = TableDedup(table)

        assert await store.is_seen(DEDUP_KEY) is False

    async def test_returns_true_when_no_metadata(self) -> None:
        """Entity without metadata attribute is treated as within TTL."""
        table = _make_table_client()
        entity: dict[str, Any] = {"ttl_seconds": DEFAULT_TTL}
        table.get_entity.return_value = entity
        store = TableDedup(table)

        assert await store.is_seen(DEDUP_KEY) is True

    async def test_auth_error_fails_closed(self) -> None:
        """Auth/permission errors return True (fail closed) to prevent reprocessing loops."""
        table = _make_table_client()
        table.get_entity.side_effect = Exception("AuthorizationPermissionMismatch: Forbidden")
        store = TableDedup(table)

        assert await store.is_seen(DEDUP_KEY) is True

    async def test_transient_error_fails_open(self) -> None:
        """Transient errors (network, timeout) return False (fail open) to allow retry."""
        table = _make_table_client()
        table.get_entity.side_effect = Exception("Connection timed out")
        store = TableDedup(table)

        assert await store.is_seen(DEDUP_KEY) is False


# -- TableDedup.mark_seen tests --


class TestTableDedupMarkSeen:
    async def test_upserts_entity_with_correct_fields(self) -> None:
        table = _make_table_client()
        store = TableDedup(table)

        await store.mark_seen(DEDUP_KEY, ttl_seconds=7200)

        table.upsert_entity.assert_called_once_with(
            {"PartitionKey": PARTITION_KEY, "RowKey": ROW_KEY, "ttl_seconds": 7200}
        )

    async def test_failure_is_non_fatal(self) -> None:
        table = _make_table_client()
        table.upsert_entity.side_effect = Exception("write failed")
        store = TableDedup(table)

        await store.mark_seen(DEDUP_KEY)  # should not raise


# -- TableDedup.aclose tests --


class TestTableDedupAclose:
    async def test_closes_table_client(self) -> None:
        table = _make_table_client()
        store = TableDedup(table)

        await store.aclose()

        table.close.assert_called_once()

    async def test_close_error_is_suppressed(self) -> None:
        table = _make_table_client()
        table.close.side_effect = Exception("close failed")
        store = TableDedup(table)

        await store.aclose()  # should not raise


# -- create_table_dedup_store factory tests --


class TestCreateTableDedupStore:
    def test_with_connection_string(self) -> None:
        with patch("azure.data.tables.TableServiceClient.from_connection_string") as mock_svc:
            mock_svc.return_value.get_table_client.return_value = MagicMock()
            result = create_table_dedup_store(None, connection_string=CONN_STR)
        assert isinstance(result, TableDedup)
        mock_svc.assert_called_once_with(CONN_STR)

    def test_with_account_url_uses_credential(self) -> None:
        with (
            patch("azure.data.tables.TableServiceClient") as mock_cls,
            patch("azure.identity.DefaultAzureCredential"),
        ):
            mock_cls.return_value.get_table_client.return_value = MagicMock()
            result = create_table_dedup_store(ACCOUNT_URL)
        assert isinstance(result, TableDedup)
        call_kwargs = mock_cls.call_args
        assert ".table." in call_kwargs.kwargs["endpoint"]

    def test_raises_without_url_or_connection_string(self) -> None:
        with pytest.raises(ValueError, match="Either account_url or connection_string"):
            create_table_dedup_store(None)
