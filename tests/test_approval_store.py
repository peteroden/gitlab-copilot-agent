"""Tests for approval_store implementations."""

import time

import pytest

from gitlab_copilot_agent.approval_store import MemoryApprovalStore
from gitlab_copilot_agent.models import PendingApproval

PROJECT_ID = 42
MR_IID = 7
REQUESTER_ID = 1


def _make_approval(
    project_id: int = PROJECT_ID,
    mr_iid: int = MR_IID,
    requester_id: int = REQUESTER_ID,
    prompt: str = "fix the bug",
    timeout: int = 3600,
) -> PendingApproval:
    """Factory for PendingApproval test instances."""
    return PendingApproval(
        task_id=f"mr-{project_id}-{mr_iid}",
        requester_id=requester_id,
        prompt=prompt,
        mr_iid=mr_iid,
        project_id=project_id,
        timeout=timeout,
    )


@pytest.mark.asyncio
async def test_memory_store_basic_flow() -> None:
    """Test storing, retrieving, and deleting an approval."""
    store = MemoryApprovalStore()
    approval = _make_approval()

    # Store
    await store.store(approval)

    # Get
    retrieved = await store.get(PROJECT_ID, MR_IID)
    assert retrieved is not None
    assert retrieved.task_id == approval.task_id
    assert retrieved.requester_id == REQUESTER_ID
    assert retrieved.prompt == "fix the bug"

    # Delete
    await store.delete(PROJECT_ID, MR_IID)
    assert await store.get(PROJECT_ID, MR_IID) is None


@pytest.mark.asyncio
async def test_memory_store_get_nonexistent() -> None:
    """Test getting a non-existent approval returns None."""
    store = MemoryApprovalStore()
    assert await store.get(PROJECT_ID, MR_IID) is None


@pytest.mark.asyncio
async def test_memory_store_delete_nonexistent() -> None:
    """Test deleting a non-existent approval does not error."""
    store = MemoryApprovalStore()
    await store.delete(PROJECT_ID, MR_IID)  # Should not raise


@pytest.mark.asyncio
async def test_memory_store_ttl_expiration() -> None:
    """Test that expired approvals are cleaned up on get()."""
    store = MemoryApprovalStore()
    approval = _make_approval(timeout=1)
    await store.store(approval)

    # Should exist immediately
    assert await store.get(PROJECT_ID, MR_IID) is not None

    # Wait for expiration
    time.sleep(1.1)

    # Should be expired and removed
    assert await store.get(PROJECT_ID, MR_IID) is None


@pytest.mark.asyncio
async def test_memory_store_overwrite() -> None:
    """Test that storing a new approval overwrites the old one."""
    store = MemoryApprovalStore()
    approval1 = _make_approval(prompt="first")
    approval2 = _make_approval(prompt="second")

    await store.store(approval1)
    await store.store(approval2)

    retrieved = await store.get(PROJECT_ID, MR_IID)
    assert retrieved is not None
    assert retrieved.prompt == "second"


@pytest.mark.asyncio
async def test_memory_store_different_mrs() -> None:
    """Test that approvals for different MRs are isolated."""
    store = MemoryApprovalStore()
    approval1 = _make_approval(mr_iid=1, prompt="first")
    approval2 = _make_approval(mr_iid=2, prompt="second")

    await store.store(approval1)
    await store.store(approval2)

    retrieved1 = await store.get(PROJECT_ID, 1)
    retrieved2 = await store.get(PROJECT_ID, 2)
    assert retrieved1 is not None
    assert retrieved1.prompt == "first"
    assert retrieved2 is not None
    assert retrieved2.prompt == "second"


@pytest.mark.asyncio
async def test_memory_store_aclose() -> None:
    """Test that aclose clears the store."""
    store = MemoryApprovalStore()
    approval = _make_approval()
    await store.store(approval)
    await store.aclose()
    assert await store.get(PROJECT_ID, MR_IID) is None
