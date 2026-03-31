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
JIRA_ISSUE_KEY = os.environ["JIRA_ISSUE_KEY"]

BRANCH_NAME = "feature/add-search-endpoint"
_FIXTURES = Path(__file__).parent / "fixtures" / "aca-mr-test"


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


# ---------------------------------------------------------------------------
# GitLab state reset
# ---------------------------------------------------------------------------


def reset_gitlab_state(project: Any) -> int:
    """Close existing MRs, recreate branch with buggy code, open fresh MR. Returns new MR iid."""
    log.info("resetting GitLab state", project=GITLAB_PROJECT, branch=BRANCH_NAME)

    for mr in project.mergerequests.list(state="opened", get_all=True):
        if mr.source_branch == BRANCH_NAME:
            mr.state_event = "close"
            mr.save()
            log.info("closed MR", iid=mr.iid)

    try:
        project.branches.delete(BRANCH_NAME)
        log.info("deleted branch", branch=BRANCH_NAME)
    except Exception:
        log.info("branch not found (already deleted)", branch=BRANCH_NAME)

    project.branches.create({"branch": BRANCH_NAME, "ref": "main"})
    log.info("created fresh branch", branch=BRANCH_NAME)

    tree = project.repository_tree(ref=BRANCH_NAME, recursive=True, get_all=True)
    existing = {item["path"] for item in tree}

    search_py = (_FIXTURES / "src/demo_app/search.py").read_text()
    main_py = (_FIXTURES / "src/demo_app/main.py").read_text()

    project.commits.create(
        {
            "branch": BRANCH_NAME,
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
            "source_branch": BRANCH_NAME,
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


def reset_jira_state() -> None:
    """Transition the test issue back to 'To Do'."""
    log.info("resetting Jira issue state", key=JIRA_ISSUE_KEY)
    with _jira_client() as client:
        resp = client.get(f"/rest/api/3/issue/{JIRA_ISSUE_KEY}/transitions")
        resp.raise_for_status()
        transitions = resp.json()["transitions"]
        to_do_id = next(
            (t["id"] for t in transitions if t["to"]["name"] == "To Do"),
            None,
        )
        if to_do_id is None:
            available = [t["to"]["name"] for t in transitions]
            raise RuntimeError(
                f"No 'To Do' transition found for {JIRA_ISSUE_KEY}. Available: {available}"
            )
        resp = client.post(
            f"/rest/api/3/issue/{JIRA_ISSUE_KEY}/transitions",
            json={"transition": {"id": to_do_id}},
        )
        resp.raise_for_status()
    log.info("Jira issue reset to 'To Do'", key=JIRA_ISSUE_KEY)


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


def test_jira_coding_flow(project: Any) -> None:
    """Verify the agent picks up the Jira issue and eventually creates an MR."""
    log.info("test: Jira→coding flow", issue=JIRA_ISSUE_KEY)

    with _jira_client() as client:

        def _issue_status() -> str:
            resp = client.get(f"/rest/api/3/issue/{JIRA_ISSUE_KEY}")
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

    # Verify a new MR was created on GitLab
    mrs = project.mergerequests.list(state="opened", source_branch=BRANCH_NAME, get_all=True)
    assert mrs, (
        f"Expected an open MR on branch '{BRANCH_NAME}' in project '{GITLAB_PROJECT}' "
        f"after Jira issue '{JIRA_ISSUE_KEY}' reached Done, but found none"
    )
    log.info("✓ Jira coding flow test passed", mr_iid=mrs[0].iid)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info(
        "starting ACA integration tests",
        project=GITLAB_PROJECT,
        jira_issue=JIRA_ISSUE_KEY,
    )

    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()
    project = gl.projects.get(GITLAB_PROJECT)

    failures: list[str] = []

    # --- Test 1: MR review ---
    try:
        mr_iid = reset_gitlab_state(project)
        test_mr_review(project, mr_iid)
    except Exception as exc:
        log.error("MR review test failed", error=str(exc))
        failures.append(f"MR review: {exc}")

    # --- Test 2: Jira → coding ---
    try:
        reset_jira_state()
        reset_gitlab_state(project)
        test_jira_coding_flow(project)
    except Exception as exc:
        log.error("Jira coding flow test failed", error=str(exc))
        failures.append(f"Jira coding flow: {exc}")

    # --- Cleanup ---
    try:
        reset_gitlab_state(project)
        reset_jira_state()
    except Exception as exc:
        log.warning("cleanup failed (non-fatal)", error=str(exc))

    if failures:
        log.error("integration tests FAILED", failures=failures)
        sys.exit(1)

    log.info("✓ all ACA integration tests passed")


if __name__ == "__main__":
    main()
