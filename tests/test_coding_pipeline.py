"""Tests for the CodingTaskRunner and CodingPipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from gitlab_copilot_agent.coding_pipeline import CodingTaskRunner
from gitlab_copilot_agent.git import TransientCloneError
from gitlab_copilot_agent.jira_models import JiraIssue, JiraIssueFields, JiraStatus
from gitlab_copilot_agent.task_executor import CodingResult, TaskExecutionError
from tests.conftest import (
    JIRA_SETTINGS,
    make_resolved_project,
    make_settings,
)

_MOD = "gitlab_copilot_agent.coding_pipeline"

_ISSUE = JiraIssue(
    id="10042",
    key="PROJ-42",
    fields=JiraIssueFields(
        summary="Add feature",
        status=JiraStatus(name="AI Ready", id="1"),
        description="Impl",
    ),
)

_MAPPING = make_resolved_project()


def _runner(
    **settings_ov: Any,
) -> tuple[CodingTaskRunner, AsyncMock, AsyncMock]:
    gl, jira = AsyncMock(), AsyncMock()
    gl.create_merge_request.return_value = 1
    return (
        CodingTaskRunner(
            make_settings(**JIRA_SETTINGS, **settings_ov),
            gl,
            jira,
            AsyncMock(),
        ),
        gl,
        jira,
    )


@pytest.fixture()
def coding_mocks(tmp_path: Path) -> dict[str, AsyncMock]:
    with (
        patch(f"{_MOD}.git_clone") as m_clone,
        patch(f"{_MOD}.git_unique_branch") as m_branch,
        patch(f"{_MOD}.git_commit") as m_commit,
        patch(f"{_MOD}.git_push") as m_push,
        patch(f"{_MOD}.run_coding_task") as m_coding,
    ):
        m_clone.return_value = tmp_path
        m_branch.return_value = "agent/proj-42"
        m_coding.return_value = CodingResult(summary="Changes made")
        yield {
            "clone": m_clone,
            "branch": m_branch,
            "commit": m_commit,
            "push": m_push,
            "coding": m_coding,
        }


# -- Happy-path tests --


@pytest.mark.parametrize(
    "auto_merge,expect_draft",
    [(False, True), (True, False)],
    ids=["draft", "ready"],
)
async def test_mr_mode(
    coding_mocks: dict[str, AsyncMock],
    auto_merge: bool,
    expect_draft: bool,
) -> None:
    runner, gl, jira = _runner(auto_merge_enabled=auto_merge)
    await runner.handle(_ISSUE, _MAPPING)

    for name in ("clone", "branch", "commit", "push"):
        coding_mocks[name].assert_awaited_once()
    gl.create_merge_request.assert_awaited_once()

    title = gl.create_merge_request.call_args[0][3]
    if expect_draft:
        assert title == "Draft: feat(proj-42): Add feature"
    else:
        assert title == "feat(proj-42): Add feature"

    comment = jira.add_comment.call_args[0][1]
    if expect_draft:
        assert "Draft MR created:" in comment
        assert "Auto-merge is disabled" in comment
        assert "un-draft the MR to enable merging" in comment
    else:
        assert "MR created:" in comment
        assert "Draft" not in comment

    assert jira.transition_issue.await_count == 2
    jira.transition_issue.assert_any_await("PROJ-42", "In Progress")
    jira.transition_issue.assert_any_await("PROJ-42", "In Review")


async def test_in_review_transition_failure_is_non_blocking(
    coding_mocks: dict[str, AsyncMock],
) -> None:
    runner, gl, jira = _runner()
    jira.transition_issue.side_effect = [None, ValueError("No transition")]
    await runner.handle(_ISSUE, _MAPPING)

    assert jira.transition_issue.await_count == 2
    gl.create_merge_request.assert_awaited_once()
    jira.add_comment.assert_awaited_once()


async def test_custom_in_review_status_used(
    coding_mocks: dict[str, AsyncMock],
) -> None:
    custom = make_resolved_project(in_review_status="QA Review")
    runner, _, jira = _runner()
    await runner.handle(_ISSUE, custom)
    jira.transition_issue.assert_any_await("PROJ-42", "QA Review")


# -- Failure-path tests --


@patch(f"{_MOD}.git_clone")
async def test_coding_failure_posts_comment_to_jira(
    mock_clone: AsyncMock,
) -> None:
    mock_clone.side_effect = Exception("Git clone failed")
    runner, _, jira = _runner()

    with pytest.raises(Exception, match="Git clone failed"):
        await runner.handle(_ISSUE, _MAPPING)

    jira.add_comment.assert_awaited_once()
    key, comment = jira.add_comment.call_args[0]
    assert key == "PROJ-42"
    assert "Automated implementation failed" in comment


@patch(f"{_MOD}.git_clone")
async def test_coding_failure_comment_posting_failure_is_logged(
    mock_clone: AsyncMock,
) -> None:
    mock_clone.side_effect = Exception("Git clone failed")
    runner, _, jira = _runner()
    jira.add_comment.side_effect = Exception("Jira API error")

    with pytest.raises(Exception, match="Git clone failed"):
        await runner.handle(_ISSUE, _MAPPING)
    jira.add_comment.assert_awaited_once()


async def test_task_execution_failure_posts_error_details(
    coding_mocks: dict[str, AsyncMock],
) -> None:
    coding_mocks["coding"].side_effect = TaskExecutionError(
        "Task failed: missing files_changed",
    )
    runner, _, jira = _runner()

    with pytest.raises(TaskExecutionError, match="missing files_changed"):
        await runner.handle(_ISSUE, _MAPPING)

    jira.add_comment.assert_awaited_once()
    comment = jira.add_comment.call_args[0][1]
    assert "Automated implementation failed" in comment
    assert "unexpected error" in comment.lower()


@patch(f"{_MOD}.git_clone")
async def test_transient_clone_failure_posts_detailed_comment(
    mock_clone: AsyncMock,
) -> None:
    mock_clone.side_effect = TransientCloneError(
        "git clone failed for https://gitlab.example.com/group/project.git"
        " after 3 attempts: The requested URL returned error: 403",
        attempts=3,
    )
    runner, _, jira = _runner()
    await runner.handle(_ISSUE, _MAPPING)

    assert jira.add_comment.await_count == 1
    key, comment = jira.add_comment.call_args[0]
    assert key == "PROJ-42"
    assert "3 attempts" in comment
    assert "transient error" in comment
    assert "retry on the next poll cycle" in comment
