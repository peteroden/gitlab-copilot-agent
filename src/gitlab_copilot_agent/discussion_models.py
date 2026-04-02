"""Pydantic models for MR discussion history.

Used by both the review pipeline (dedup, resolution, incremental review)
and the discussion handler (Q&A, coding via @mention).  Models map
closely to the GitLab Discussions API response structure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DiscussionNote(BaseModel):
    """A single note within a discussion thread."""

    model_config = ConfigDict(frozen=True)

    note_id: int = Field(description="GitLab note ID")
    author_id: int = Field(description="Author's GitLab user ID")
    author_username: str = Field(description="Author's GitLab username (for display)")
    body: str = Field(description="Note body text")
    created_at: str = Field(description="ISO 8601 timestamp")
    is_system: bool = Field(description="True for system-generated notes")
    resolved: bool | None = Field(
        default=None, description="Resolution status (None if not resolvable)"
    )
    resolvable: bool = Field(default=False, description="Whether the note can be resolved")
    position: dict[str, object] | None = Field(
        default=None,
        description="Diff position: new_path, old_path, new_line, old_line",
    )


class Discussion(BaseModel):
    """A threaded discussion on a merge request."""

    model_config = ConfigDict(frozen=True)

    discussion_id: str = Field(description="GitLab discussion ID")
    notes: list[DiscussionNote] = Field(description="Notes in thread order")
    is_resolved: bool = Field(default=False, description="Whether the discussion is resolved")
    is_inline: bool = Field(
        default=False, description="True for DiffNote (inline), False for overview"
    )


class AgentIdentity(BaseModel):
    """The agent's GitLab identity, discovered via GET /user."""

    model_config = ConfigDict(frozen=True)

    user_id: int = Field(description="Immutable GitLab user ID")
    username: str = Field(description="GitLab username (mutable, for display/@mention)")


class DiscussionHistory(BaseModel):
    """Full discussion context for an MR, including agent identity."""

    discussions: list[Discussion] = Field(
        default_factory=lambda: [], description="All discussions"
    )
    agent: AgentIdentity = Field(description="Agent identity for self-detection")


# Resolve forward references for Pydantic v2 + `from __future__ import annotations`.
# Without this, pytest-cov instrumentation can break class identity checks.
DiscussionNote.model_rebuild()
Discussion.model_rebuild()
AgentIdentity.model_rebuild()
DiscussionHistory.model_rebuild()
