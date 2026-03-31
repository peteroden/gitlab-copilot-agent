#!/usr/bin/env python3
"""ACA integration tests — verify MR review and Jira→coding flows against real services."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import gitlab
import httpx
import structlog

log = structlog.get_logger()

# --- Environment ---
GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.com")
GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
GITLAB_PROJECT = os.environ["GITLAB_PROJECT"]
JIRA_URL = os.environ["JIRA_URL"]
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_ISSUE_KEY = os.environ.get("JIRA_ISSUE_KEY")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY")
JIRA_TRIGGER_STATUS = os.environ.get("JIRA_TRIGGER_STATUS", "AI Ready")

REVIEW_BRANCH_NAME = "feature/add-search-endpoint"
_FIXTURES = Path(__file__).parent / "fixtures" / "aca-mr-test"
TEST_ISSUE_SUMMARY = "Add input validation to the blog API search endpoint"
TEST_ISSUE_DESCRIPTION = (
    "Add validation for the /search endpoint so blank queries are rejected and very long "
    "queries are bounded.\n\n"
    "Acceptance Criteria:\n"
    "- Reject empty or whitespace-only search queries\n"
    "- Reject search queries longer than 100 characters\n"
    "- Return a clear 400 response for invalid input\n"
    "- Add or update tests for the validation behavior"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jira_client() -> httpx.Client:
    return httpx.Client(
        base_url=JIRA_URL,
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )


def _poll(
    description: str,
    check_fn: Any,
    timeout_s: int,
    interval_s: int = 15,
) -> Any:
    """Poll `check_fn()` until it returns a truthy value or timeout expires."""
    deadline = time.monotonic() + timeout_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        log.info("polling", description=description, attempt=attempt)
        result = check_fn()
        if result:
            return result
        time.sleep(interval_s)
    raise TimeoutError(f"Timed out after {timeout_s}s waiting for: {description}")


def _require_jira_project_key() -> str:
    if JIRA_PROJECT_KEY:
        return JIRA_PROJECT_KEY
    if JIRA_ISSUE_KEY:
        return JIRA_ISSUE_KEY.split("-", 1)[0]
    raise RuntimeError("Set either JIRA_PROJECT_KEY or JIRA_ISSUE_KEY for ACA integration tests")


def _coding_branch_prefix(issue_key: str) -> str:
    return f"agent/{issue_key.lower()}"


def _coding_project_branch_prefix(project_key: str) -> str:
    return f"agent/{project_key.lower()}-"


def _matches_coding_branch(issue_key: str, branch_name: str) -> bool:
    prefix = _coding_branch_prefix(issue_key)
    return branch_name == prefix or branch_name.startswith(f"{prefix}-")


def _list_open_coding_mrs(project: Any, issue_key: str) -> list[Any]:
    return [
        mr
        for mr in project.mergerequests.list(state="opened", get_all=True)
        if _matches_coding_branch(issue_key, mr.source_branch)
    ]


def _transition_name(transition: dict[str, Any]) -> str:
    to = transition.get("to")
    if isinstance(to, dict):
        name = to.get("name")
        if isinstance(name, str):
            return name
    name = transition.get("name")
    return name if isinstance(name, str) else ""


def _make_adf(text: str) -> dict[str, Any]:
    paragraphs = [
        paragraph.strip() for paragraph in text.strip().split("\n\n") if paragraph.strip()
    ]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": paragraph}],
            }
            for paragraph in paragraphs
        ],
    }


def create_jira_issue() -> str:
    """Create a fresh Jira issue for the coding-flow portion of the integration test."""
    project_key = _require_jira_project_key()
    with _jira_client() as client:
        resp = client.post(
            "/rest/api/3/issue",
            json={
                "fields": {
                    "project": {"key": project_key},
                    "summary": TEST_ISSUE_SUMMARY,
                    "description": _make_adf(TEST_ISSUE_DESCRIPTION),
                    "issuetype": {"name": "Task"},
                }
            },
        )
        resp.raise_for_status()
    issue_key = resp.json()["key"]
    log.info("created Jira issue", key=issue_key, project=project_key)
    return issue_key


def delete_jira_issue(issue_key: str) -> None:
    """Delete a Jira issue created for this integration test run."""
    with _jira_client() as client:
        resp = client.delete(f"/rest/api/3/issue/{issue_key}")
        resp.raise_for_status()
    log.info("deleted Jira issue", key=issue_key)


# ---------------------------------------------------------------------------
# GitLab state management
# ---------------------------------------------------------------------------


def cleanup_review_gitlab_state(project: Any) -> None:
    """Close the review MR and delete its branch."""
    log.info("cleaning review GitLab state", project=GITLAB_PROJECT)

    for mr in project.mergerequests.list(state="opened", get_all=True):
        if mr.source_branch == REVIEW_BRANCH_NAME:
            mr.state_event = "close"
            mr.save()
            log.info("closed MR", iid=mr.iid, branch=mr.source_branch)

    for branch in project.branches.list(get_all=True):
        if branch.name == REVIEW_BRANCH_NAME:
            project.branches.delete(branch.name)
            log.info("deleted branch", branch=branch.name)


def cleanup_coding_gitlab_state(project: Any, issue_key: str) -> None:
    """Close the coding MR for one Jira issue and delete its branch."""
    log.info("cleaning coding GitLab state", project=GITLAB_PROJECT, issue_key=issue_key)
    coding_prefix = _coding_branch_prefix(issue_key)

    for mr in project.mergerequests.list(state="opened", get_all=True):
        if mr.source_branch == coding_prefix or mr.source_branch.startswith(f"{coding_prefix}-"):
            mr.state_event = "close"
            mr.save()
            log.info("closed MR", iid=mr.iid, branch=mr.source_branch)

    for branch in project.branches.list(get_all=True):
        if branch.name == coding_prefix or branch.name.startswith(f"{coding_prefix}-"):
            project.branches.delete(branch.name)
            log.info("deleted branch", branch=branch.name)


def cleanup_gitlab_state(project: Any, issue_key: str | None = None) -> None:
    """Close test MRs and delete test branches without recreating them."""
    cleanup_review_gitlab_state(project)
    if issue_key is not None:
        cleanup_coding_gitlab_state(project, issue_key)


def prepare_review_mr(project: Any) -> int:
    """Create a fresh MR with the intentionally buggy review fixture."""
    cleanup_gitlab_state(project)
    log.info("preparing review MR", project=GITLAB_PROJECT, branch=REVIEW_BRANCH_NAME)

    project.branches.create({"branch": REVIEW_BRANCH_NAME, "ref": "main"})
    log.info("created fresh branch", branch=REVIEW_BRANCH_NAME)

    tree = project.repository_tree(ref=REVIEW_BRANCH_NAME, recursive=True, get_all=True)
    existing = {item["path"] for item in tree}

    search_py = (_FIXTURES / "src/demo_app/search.py").read_text()
    main_py = (_FIXTURES / "src/demo_app/main.py").read_text()

    project.commits.create(
        {
            "branch": REVIEW_BRANCH_NAME,
            "commit_message": "Add search endpoint with keyword matching",
            "actions": [
                {
                    "action": "create",
                    "file_path": "src/demo_app/search.py",
                    "content": search_py,
                },
                {
                    "action": "update" if "src/demo_app/main.py" in existing else "create",
                    "file_path": "src/demo_app/main.py",
                    "content": main_py,
                },
            ],
        }
    )
    log.info("pushed buggy files to branch")

    mr = project.mergerequests.create(
        {
            "source_branch": REVIEW_BRANCH_NAME,
            "target_branch": "main",
            "title": "Add post search endpoint",
            "description": (
                "Adds a search endpoint. Has intentional issues for the agent to review."
            ),
        }
    )
    log.info("created MR", iid=mr.iid, url=mr.web_url)
    return mr.iid


# ---------------------------------------------------------------------------
# Jira state reset
# ---------------------------------------------------------------------------


def transition_jira_issue(issue_key: str, target_status: str) -> None:
    """Transition the test issue to the requested Jira status."""
    log.info("transitioning Jira issue", key=issue_key, target_status=target_status)
    with _jira_client() as client:
        resp = client.get(f"/rest/api/3/issue/{issue_key}/transitions")
        resp.raise_for_status()
        transitions = resp.json()["transitions"]
        transition_id = next(
            (t["id"] for t in transitions if _transition_name(t) == target_status),
            None,
        )
        if transition_id is None:
            available = [_transition_name(t) for t in transitions]
            raise RuntimeError(
                f"No '{target_status}' transition found for {issue_key}. Available: {available}"
            )
        resp = client.post(
            f"/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": transition_id}},
        )
        resp.raise_for_status()
    log.info("Jira issue transitioned", key=issue_key, target_status=target_status)


def reset_jira_state(issue_key: str) -> None:
    """Transition the test issue back to 'To Do'."""
    transition_jira_issue(issue_key, "To Do")


def trigger_jira_issue(issue_key: str) -> None:
    """Move the test issue into the configured trigger status."""
    transition_jira_issue(issue_key, JIRA_TRIGGER_STATUS)


# ---------------------------------------------------------------------------
# Test: MR review
# ---------------------------------------------------------------------------


def test_mr_review(project: Any, mr_iid: int) -> None:
    """Verify the agent posts at least one review comment on the MR."""
    log.info("test: MR review", mr_iid=mr_iid)

    def _has_discussions() -> bool:
        discussions = project.mergerequests.get(mr_iid).discussions.list(get_all=True)
        non_system = [d for d in discussions if not d.attributes.get("individual_note")]
        if non_system:
            log.info("found review discussions", count=len(non_system))
            return True
        return False

    _poll("MR review comment appears", _has_discussions, timeout_s=300, interval_s=15)
    log.info("✓ MR review test passed", mr_iid=mr_iid)


# ---------------------------------------------------------------------------
# Test: Jira → coding flow
# ---------------------------------------------------------------------------


def test_jira_coding_flow(project: Any, issue_key: str, existing_mr_ids: set[int]) -> None:
    """Verify the agent picks up the Jira issue and eventually creates an MR."""
    log.info("test: Jira→coding flow", issue=issue_key)

    with _jira_client() as client:

        def _issue_status() -> str:
            resp = client.get(f"/rest/api/3/issue/{issue_key}")
            resp.raise_for_status()
            return resp.json()["fields"]["status"]["name"]

        def _reached_in_progress() -> bool:
            status = _issue_status()
            log.info("Jira issue status", status=status)
            return status in ("In Progress", "Done")

        def _reached_done() -> bool:
            status = _issue_status()
            log.info("Jira issue status", status=status)
            return status == "Done"

    _poll("Jira issue reaches In Progress", _reached_in_progress, timeout_s=300)
    _poll("Jira issue reaches Done", _reached_done, timeout_s=480)

    def _new_coding_mr() -> Any:
        for mr in _list_open_coding_mrs(project, issue_key):
            if mr.iid not in existing_mr_ids:
                log.info("found coding MR", iid=mr.iid, branch=mr.source_branch)
                return mr
        return None

    mr = _poll("GitLab coding MR appears", _new_coding_mr, timeout_s=300)
    log.info("✓ Jira coding flow test passed", mr_iid=mr.iid)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info(
        "starting ACA integration tests",
        project=GITLAB_PROJECT,
        jira_issue=JIRA_ISSUE_KEY,
        jira_project=_require_jira_project_key(),
    )

    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()
    project = gl.projects.get(GITLAB_PROJECT)

    failures: list[str] = []
    created_jira_issue = False

    # --- Test 1: MR review ---
    try:
        mr_iid = prepare_review_mr(project)
        test_mr_review(project, mr_iid)
    except Exception as exc:
        log.error("MR review test failed", error=str(exc))
        failures.append(f"MR review: {exc}")

    # --- Test 2: Jira → coding ---
    jira_issue_key = JIRA_ISSUE_KEY
    try:
        if jira_issue_key is None:
            jira_issue_key = create_jira_issue()
            created_jira_issue = True
        else:
            reset_jira_state(jira_issue_key)
        cleanup_gitlab_state(project, jira_issue_key)
        existing_mr_ids = {mr.iid for mr in _list_open_coding_mrs(project, jira_issue_key)}
        trigger_jira_issue(jira_issue_key)
        test_jira_coding_flow(project, jira_issue_key, existing_mr_ids)
    except Exception as exc:
        log.error("Jira coding flow test failed", error=str(exc))
        failures.append(f"Jira coding flow: {exc}")

    # --- Cleanup ---
    try:
        cleanup_gitlab_state(project, jira_issue_key)
        if JIRA_ISSUE_KEY:
            reset_jira_state(JIRA_ISSUE_KEY)
        elif created_jira_issue and jira_issue_key is not None:
            delete_jira_issue(jira_issue_key)
    except Exception as exc:
        log.warning("cleanup failed (non-fatal)", error=str(exc))

    if failures:
        log.error("integration tests FAILED", failures=failures)
        sys.exit(1)

    log.info("✓ all ACA integration tests passed")


if __name__ == "__main__":
    main()
