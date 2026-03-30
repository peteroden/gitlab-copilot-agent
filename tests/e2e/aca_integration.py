#!/usr/bin/env python3
"""ACA integration tests — runs against a live dev ACA environment.

Tests:
  1. MR review flow: reset GitLab state, wait for agent to post review discussion.
  2. Jira → coding flow: reset Jira issue to "To Do", wait for agent to
     transition to "In Progress" then "Done" and create a GitLab MR.

Environment variables:
  CONTROLLER_FQDN   FQDN of the deployed ACA controller (no scheme)
  GITLAB_URL        GitLab instance base URL
  GITLAB_TOKEN      GitLab personal access token
  GITLAB_PROJECT    GitLab project path (e.g. peteroden/e2e-aca-test)
  JIRA_URL          Jira instance base URL
  JIRA_EMAIL        Jira user email
  JIRA_API_TOKEN    Jira API token
  JIRA_ISSUE_KEY    Jira issue key to use for the coding flow test (e.g. E2EACA-1)
"""

from __future__ import annotations

import base64
import os
import sys
import time
from collections.abc import Callable

import httpx
import structlog

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONTROLLER_FQDN = os.environ["CONTROLLER_FQDN"]
GITLAB_URL = os.environ["GITLAB_URL"].rstrip("/")
GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
GITLAB_PROJECT = os.environ["GITLAB_PROJECT"]
JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_ISSUE_KEY = os.environ["JIRA_ISSUE_KEY"]

BRANCH_NAME = "feature/add-search-endpoint"

MR_REVIEW_TIMEOUT = 300  # seconds
JIRA_FLOW_TIMEOUT = 480  # seconds
POLL_INTERVAL = 10  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gitlab_headers() -> dict[str, str]:
    return {"PRIVATE-TOKEN": GITLAB_TOKEN}


def _jira_headers() -> dict[str, str]:
    credentials = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
    }


def poll_until(
    check: Callable[[], bool],
    label: str,
    timeout: int = MR_REVIEW_TIMEOUT,
    interval: int = POLL_INTERVAL,
) -> None:
    """Poll until *check* returns True or *timeout* seconds elapse."""
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if check():
            log.info(f"{label} ✓ (attempt {attempt})")
            return
        remaining = deadline - time.monotonic()
        log.debug(f"{label} — waiting ({remaining:.0f}s remaining)")
        time.sleep(interval)
    raise TimeoutError(f"{label}: timed out after {timeout}s")


# ---------------------------------------------------------------------------
# GitLab helpers
# ---------------------------------------------------------------------------


def gitlab_get_project_id(client: httpx.Client) -> int:
    encoded = GITLAB_PROJECT.replace("/", "%2F")
    resp = client.get(
        f"{GITLAB_URL}/api/v4/projects/{encoded}",
        headers=_gitlab_headers(),
    )
    resp.raise_for_status()
    return resp.json()["id"]


def reset_gitlab_state(client: httpx.Client, project_id: int) -> int:
    """Close existing MRs on the branch, delete and recreate branch with buggy code.
    Returns the new MR iid."""
    # Close open MRs
    resp = client.get(
        f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests",
        headers=_gitlab_headers(),
        params={"state": "opened", "source_branch": BRANCH_NAME},
    )
    resp.raise_for_status()
    for mr in resp.json():
        client.put(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr['iid']}",
            headers=_gitlab_headers(),
            json={"state_event": "close"},
        ).raise_for_status()
        log.info("closed MR", iid=mr["iid"])

    # Delete branch (ignore 404)
    del_resp = client.delete(
        f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/branches/{BRANCH_NAME}",
        headers=_gitlab_headers(),
    )
    if del_resp.status_code not in (200, 204, 404):
        del_resp.raise_for_status()

    # Recreate branch from main
    client.post(
        f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/branches",
        headers=_gitlab_headers(),
        json={"branch": BRANCH_NAME, "ref": "main"},
    ).raise_for_status()
    log.info("created branch", branch=BRANCH_NAME)

    # Check existing files
    resp = client.get(
        f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/tree",
        headers=_gitlab_headers(),
        params={"ref": BRANCH_NAME, "recursive": True, "per_page": 100},
    )
    resp.raise_for_status()
    existing = {item["path"] for item in resp.json()}

    search_py = '''\
"""Search functionality for the Blog Post API."""

from demo_app.database import _get_connection


def search_posts(query):
    conn = _get_connection()
    results = conn.execute(
        f"SELECT * FROM posts WHERE title LIKE \'%{query}%\'"
        f" OR content LIKE \'%{query}%\'"
    ).fetchall()
    conn.close()
    return [dict(row) for row in results]


def search_by_date(start, end):
    conn = _get_connection()
    results = conn.execute(
        f"SELECT * FROM posts WHERE created_at BETWEEN \'{start}\' AND \'{end}\'"
    ).fetchall()
    conn.close()
    return results
'''

    actions = [
        {
            "action": "create",
            "file_path": "src/demo_app/search.py",
            "content": search_py,
        },
    ]
    if "src/demo_app/main.py" in existing:
        actions[0]["action"] = "update"  # update if search.py already exists

    client.post(
        f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/commits",
        headers=_gitlab_headers(),
        json={
            "branch": BRANCH_NAME,
            "commit_message": "Add search endpoint with keyword matching",
            "actions": actions,
        },
    ).raise_for_status()
    log.info("pushed buggy code")

    mr_resp = client.post(
        f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests",
        headers=_gitlab_headers(),
        json={
            "source_branch": BRANCH_NAME,
            "target_branch": "main",
            "title": "Add post search endpoint",
            "description": (
                "Adds a search endpoint to find posts by keyword.\n\n"
                "This MR has intentional issues for the agent to review."
            ),
        },
    )
    mr_resp.raise_for_status()
    iid: int = mr_resp.json()["iid"]
    log.info("created MR", iid=iid)
    return iid


# ---------------------------------------------------------------------------
# Jira helpers
# ---------------------------------------------------------------------------


def reset_jira_issue(client: httpx.Client) -> None:
    """Transition the Jira issue back to 'To Do'."""
    resp = client.get(
        f"{JIRA_URL}/rest/api/3/issue/{JIRA_ISSUE_KEY}/transitions",
        headers=_jira_headers(),
    )
    resp.raise_for_status()
    transitions = resp.json().get("transitions", [])
    todo_id = next(
        (t["id"] for t in transitions if t["to"]["name"].lower() == "to do"),
        None,
    )
    if todo_id is None:
        raise RuntimeError(
            f"Could not find 'To Do' transition for {JIRA_ISSUE_KEY}. "
            f"Available: {[t['to']['name'] for t in transitions]}"
        )
    client.post(
        f"{JIRA_URL}/rest/api/3/issue/{JIRA_ISSUE_KEY}/transitions",
        headers=_jira_headers(),
        json={"transition": {"id": todo_id}},
    ).raise_for_status()
    log.info("reset Jira issue to To Do", key=JIRA_ISSUE_KEY)


def get_jira_status(client: httpx.Client) -> str:
    resp = client.get(
        f"{JIRA_URL}/rest/api/3/issue/{JIRA_ISSUE_KEY}",
        headers=_jira_headers(),
        params={"fields": "status"},
    )
    resp.raise_for_status()
    return resp.json()["fields"]["status"]["name"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mr_review(gl_client: httpx.Client, project_id: int) -> None:
    """Verify the agent posts at least one review discussion on the new MR."""
    log.info("=== Test 1: MR review flow ===")
    mr_iid = reset_gitlab_state(gl_client, project_id)
    log.info("waiting for agent to review MR", iid=mr_iid)

    def has_discussions() -> bool:
        resp = gl_client.get(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions",
            headers=_gitlab_headers(),
        )
        if resp.status_code != 200:
            return False
        discussions = [d for d in resp.json() if not d.get("individual_note")]
        return len(discussions) > 0

    poll_until(has_discussions, "MR review discussion", timeout=MR_REVIEW_TIMEOUT)
    log.info("✅ Test 1 passed: agent posted MR review discussion")


def test_jira_coding_flow(
    gl_client: httpx.Client, jira_client: httpx.Client, project_id: int
) -> None:
    """Verify the agent picks up the Jira issue and creates a GitLab MR."""
    log.info("=== Test 2: Jira → coding flow ===")
    reset_jira_issue(jira_client)
    log.info("waiting for agent to pick up Jira issue and create MR")

    def reached_in_progress() -> bool:
        return get_jira_status(jira_client).lower() in ("in progress", "done")

    def reached_done_with_mr() -> bool:
        status = get_jira_status(jira_client)
        if status.lower() != "done":
            return False
        # Verify a new MR was created
        resp = gl_client.get(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/merge_requests",
            headers=_gitlab_headers(),
            params={"state": "opened", "order_by": "created_at", "sort": "desc", "per_page": 5},
        )
        return resp.status_code == 200 and len(resp.json()) > 0

    poll_until(reached_in_progress, "Jira In Progress", timeout=JIRA_FLOW_TIMEOUT // 2)
    poll_until(reached_done_with_mr, "Jira Done + MR created", timeout=JIRA_FLOW_TIMEOUT)
    log.info("✅ Test 2 passed: Jira issue reached Done and MR was created")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log.info(
        "ACA integration tests starting",
        controller=CONTROLLER_FQDN,
        project=GITLAB_PROJECT,
        jira_issue=JIRA_ISSUE_KEY,
    )

    with (
        httpx.Client(timeout=30) as gl_client,
        httpx.Client(timeout=30) as jira_client,
    ):
        project_id = gitlab_get_project_id(gl_client)
        log.info("resolved project", project_id=project_id)

        failures: list[str] = []

        for test_fn, args in [
            (test_mr_review, (gl_client, project_id)),
            (test_jira_coding_flow, (gl_client, jira_client, project_id)),
        ]:
            try:
                test_fn(*args)
            except Exception as exc:
                log.error(f"{test_fn.__name__} FAILED", error=str(exc))
                failures.append(test_fn.__name__)

    if failures:
        log.error("ACA integration tests FAILED", failed=failures)
        sys.exit(1)

    log.info("✅ All ACA integration tests passed")


if __name__ == "__main__":
    main()
