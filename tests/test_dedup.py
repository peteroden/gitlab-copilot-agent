"""Tests for DeduplicationService — unified dedup for review, note, and issue events."""

from __future__ import annotations

import pytest

from gitlab_copilot_agent.concurrency import MemoryDedup
from gitlab_copilot_agent.dedup import DeduplicationService

PROJECT_ID = 42
MR_IID = 7
HEAD_SHA = "abc123"
NOTE_ID = 999
ISSUE_KEY = "PROJ-42"


def _svc(*, review_on_push: bool = True) -> DeduplicationService:
    return DeduplicationService(MemoryDedup(), review_on_push=review_on_push)


# -- Mark-then-check round-trips (parametrized) ----------------------------


@pytest.mark.parametrize(
    ("mark", "check", "expected"),
    [
        pytest.param("review", "review_same", True, id="review-seen-after-mark"),
        pytest.param("review", "review_other_sha", False, id="review-different-sha-unseen"),
        pytest.param("note", "note_same", True, id="note-seen-after-mark"),
        pytest.param("note", "note_other", False, id="note-different-id-unseen"),
        pytest.param("issue", "issue_same", True, id="issue-seen-after-mark"),
    ],
)
async def test_mark_then_check(mark: str, check: str, expected: bool) -> None:
    svc = _svc()
    # Mark
    if mark == "review":
        await svc.mark_review(PROJECT_ID, MR_IID, HEAD_SHA)
    elif mark == "note":
        await svc.mark_note(PROJECT_ID, MR_IID, NOTE_ID)
    elif mark == "issue":
        await svc.mark_issue(ISSUE_KEY)
    # Check
    if check == "review_same":
        result = await svc.is_review_seen(PROJECT_ID, MR_IID, HEAD_SHA)
    elif check == "review_other_sha":
        result = await svc.is_review_seen(PROJECT_ID, MR_IID, "other_sha")
    elif check == "note_same":
        result = await svc.is_note_seen(PROJECT_ID, MR_IID, NOTE_ID)
    elif check == "note_other":
        result = await svc.is_note_seen(PROJECT_ID, MR_IID, NOTE_ID + 1)
    elif check == "issue_same":
        result = await svc.is_issue_seen(ISSUE_KEY)
    else:
        raise AssertionError(f"unknown check: {check}")
    assert result is expected


@pytest.mark.parametrize(
    ("method", "args"),
    [
        pytest.param("is_review_seen", (PROJECT_ID, MR_IID, HEAD_SHA), id="review"),
        pytest.param("is_note_seen", (PROJECT_ID, MR_IID, NOTE_ID), id="note"),
        pytest.param("is_issue_seen", (ISSUE_KEY,), id="issue"),
    ],
)
async def test_unseen_returns_false(method: str, args: tuple[object, ...]) -> None:
    """All check methods return False for keys never marked."""
    svc = _svc()
    result = await getattr(svc, method)(*args)
    assert result is False


# -- review_on_push key strategy -------------------------------------------


async def test_review_on_push_false_ignores_sha() -> None:
    """Without review_on_push, any SHA matches a marked MR."""
    svc = _svc(review_on_push=False)
    await svc.mark_review(PROJECT_ID, MR_IID, HEAD_SHA)
    assert await svc.is_review_seen(PROJECT_ID, MR_IID, "completely_different")


# -- Local cache vs shared backend -----------------------------------------


async def test_local_cache_hit_avoids_backend() -> None:
    """Review mark populates local cache; backend can be cleared without effect."""
    backend = MemoryDedup()
    svc = DeduplicationService(backend, review_on_push=True)
    await svc.mark_review(PROJECT_ID, MR_IID, HEAD_SHA)
    backend._seen.clear()
    assert await svc.is_review_seen(PROJECT_ID, MR_IID, HEAD_SHA)


async def test_backend_hit_on_local_miss() -> None:
    """Cross-trigger: poller marks backend directly, webhook finds it via fallthrough."""
    backend = MemoryDedup()
    svc = DeduplicationService(backend, review_on_push=True)
    await backend.mark_seen(f"review:{PROJECT_ID}:{MR_IID}:{HEAD_SHA}", ttl_seconds=86400)
    assert await svc.is_review_seen(PROJECT_ID, MR_IID, HEAD_SHA)


async def test_backend_error_treated_as_miss() -> None:
    """Backend failure on review check fails open (doesn't block webhook intake)."""
    backend = MemoryDedup()
    svc = DeduplicationService(backend, review_on_push=True)

    async def exploding_is_seen(key: str) -> bool:
        raise RuntimeError("backend down")

    backend.is_seen = exploding_is_seen  # type: ignore[assignment]
    assert not await svc.is_review_seen(PROJECT_ID, MR_IID, HEAD_SHA)


async def test_issue_uses_shared_backend() -> None:
    """Issue marks go through the shared backend (cross-node visible)."""
    backend = MemoryDedup()
    svc = DeduplicationService(backend, review_on_push=True)
    await svc.mark_issue(ISSUE_KEY)
    assert await backend.is_seen(f"jira:{ISSUE_KEY}")


# -- Key format ------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "args", "expected"),
    [
        pytest.param("_review_key", (42, 7, "abc"), "review:42:7:abc", id="review-on-push"),
        pytest.param("_note_key", (42, 7, 999), "note:42:7:999", id="note"),
        pytest.param("_issue_key", ("PROJ-42",), "jira:PROJ-42", id="issue"),
    ],
)
def test_key_format(method: str, args: tuple[object, ...], expected: str) -> None:
    svc = _svc()
    assert getattr(svc, method)(*args) == expected


def test_review_key_without_push_omits_sha() -> None:
    svc = _svc(review_on_push=False)
    assert svc._review_key(42, 7, "abc") == "review:42:7"


# -- Lifecycle -------------------------------------------------------------


async def test_aclose_no_error() -> None:
    svc = _svc()
    await svc.mark_review(PROJECT_ID, MR_IID, HEAD_SHA)
    await svc.mark_issue(ISSUE_KEY)
    await svc.aclose()
