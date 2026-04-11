"""Tests for TaskEvent and ScheduledTask models."""

from typing import Any

import pytest
from pydantic import ValidationError

from gitlab_copilot_agent.events import ScheduledTask
from tests.conftest import GITLAB_TOKEN, MR_IID, PROJECT_ID, make_task_event

_CODING_OPTIONAL = {"task_type": "coding", "mr_iid": None, "head_sha": None}


# -- Construction --


@pytest.mark.parametrize(
    ("overrides", "field", "expected"),
    [
        ({}, "task_type", "review"),
        ({}, "project_id", PROJECT_ID),
        ({}, "mr_iid", MR_IID),
        ({}, "head_sha", "abc123"),
        ({"task_type": "discussion", "head_sha": None, "note_id": 99}, "note_id", 99),
        ({**_CODING_OPTIONAL, "jira_issue_key": "P-1"}, "jira_issue_key", "P-1"),
        (_CODING_OPTIONAL, "mr_iid", None),
    ],
    ids=[
        "review-type",
        "project-id",
        "mr-iid",
        "head-sha",
        "discussion-note-id",
        "coding-jira-key",
        "coding-no-mr",
    ],
)
def test_task_event_field_access(overrides: dict[str, Any], field: str, expected: object) -> None:
    assert getattr(make_task_event(**overrides), field) == expected


def test_frozen_model_prevents_mutation() -> None:
    with pytest.raises(ValidationError):
        make_task_event().project_id = 999  # type: ignore[misc]


# -- Validation: required fields per task_type --

_DISC_BASE = {"task_type": "discussion", "head_sha": None}


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"mr_iid": None}, "review events require mr_iid"),
        ({"head_sha": None}, "review events require head_sha"),
        ({**_DISC_BASE, "mr_iid": None, "note_id": 1}, "discussion events require mr_iid"),
        (_DISC_BASE, "discussion events require note_id"),
    ],
    ids=["review-no-mr", "review-no-sha", "discussion-no-mr", "discussion-no-note"],
)
def test_task_event_rejects_invalid(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        make_task_event(**overrides)


def test_coding_allows_all_optional() -> None:
    assert make_task_event(**_CODING_OPTIONAL).task_type == "coding"


# -- S1: token never leaks through serialization --


@pytest.mark.parametrize(
    "serialize",
    [
        lambda e: e.model_dump(),
        dict,
        lambda e: e.log_safe(),
    ],
    ids=["model_dump", "dict", "log_safe"],
)
def test_token_excluded_from_dict_output(serialize: Any) -> None:
    assert "token" not in serialize(make_task_event())


@pytest.mark.parametrize(
    "to_str",
    [lambda e: e.model_dump_json(), repr],
    ids=["json", "repr"],
)
def test_token_excluded_from_string_output(to_str: Any) -> None:
    assert GITLAB_TOKEN not in to_str(make_task_event())


def test_token_accessible_for_execution() -> None:
    assert make_task_event().token == GITLAB_TOKEN


# -- log_safe preserves non-secret data --


def test_log_safe_contains_expected_fields() -> None:
    safe = make_task_event().log_safe()
    assert safe["project_id"] == PROJECT_ID
    assert safe["task_type"] == "review"


# -- ScheduledTask --


def test_scheduled_task_wraps_event() -> None:
    event = make_task_event()
    task = ScheduledTask(event=event, dedup_key="review:42:7:abc")
    assert task.event is event
    assert task.dedup_key == "review:42:7:abc"
    assert task.trace_id == ""


def test_scheduled_task_frozen() -> None:
    with pytest.raises(ValidationError):
        ScheduledTask(event=make_task_event(), dedup_key="x").dedup_key = "y"  # type: ignore[misc]
