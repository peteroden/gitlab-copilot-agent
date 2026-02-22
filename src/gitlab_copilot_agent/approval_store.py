"""Storage for pending /copilot approvals — Protocol + Memory + Redis implementations."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from redis.asyncio import Redis

from gitlab_copilot_agent.models import PendingApproval

_APPROVAL_PREFIX = "approval:"


@runtime_checkable
class ApprovalStore(Protocol):
    """Protocol for storing pending approvals."""

    async def store(self, approval: PendingApproval) -> None: ...

    async def get(self, project_id: int, mr_iid: int) -> PendingApproval | None: ...

    async def delete(self, project_id: int, mr_iid: int) -> None: ...

    async def aclose(self) -> None: ...


class MemoryApprovalStore:
    """In-memory approval store with TTL checking."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[PendingApproval, float]] = {}

    def _key(self, project_id: int, mr_iid: int) -> str:
        return f"{project_id}:{mr_iid}"

    async def store(self, approval: PendingApproval) -> None:
        key = self._key(approval.project_id, approval.mr_iid)
        expires_at = approval.created_at + approval.timeout
        self._data[key] = (approval, expires_at)

    async def get(self, project_id: int, mr_iid: int) -> PendingApproval | None:
        key = self._key(project_id, mr_iid)
        entry = self._data.get(key)
        if entry is None:
            return None
        approval, expires_at = entry
        if time.time() > expires_at:
            del self._data[key]
            return None
        return approval

    async def delete(self, project_id: int, mr_iid: int) -> None:
        key = self._key(project_id, mr_iid)
        self._data.pop(key, None)

    async def aclose(self) -> None:
        self._data.clear()


class RedisApprovalStore:
    """Redis-backed approval store with automatic TTL expiry."""

    def __init__(self, client: Redis) -> None:
        self._client: Redis = client

    def _key(self, project_id: int, mr_iid: int) -> str:
        return f"{_APPROVAL_PREFIX}{project_id}:{mr_iid}"

    async def store(self, approval: PendingApproval) -> None:
        key = self._key(approval.project_id, approval.mr_iid)
        value = approval.model_dump_json()
        await self._client.set(key, value, ex=approval.timeout)

    async def get(self, project_id: int, mr_iid: int) -> PendingApproval | None:
        key = self._key(project_id, mr_iid)
        val = await self._client.get(key)
        if val is None:
            return None
        data = val.decode() if isinstance(val, bytes) else str(val)
        return PendingApproval.model_validate_json(data)

    async def delete(self, project_id: int, mr_iid: int) -> None:
        key = self._key(project_id, mr_iid)
        await self._client.delete(key)

    async def aclose(self) -> None:
        await self._client.aclose()


def create_approval_store(backend: str, redis_url: str | None = None) -> ApprovalStore:
    """Factory: create an ApprovalStore for the given backend."""
    if backend == "redis":
        import redis.asyncio as aioredis

        if not redis_url:
            msg = "redis_url is required when backend='redis'"
            raise ValueError(msg)
        return RedisApprovalStore(aioredis.from_url(redis_url))
    return MemoryApprovalStore()
