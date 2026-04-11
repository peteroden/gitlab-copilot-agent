#!/usr/bin/env python3
"""ACA integration tests — verify MR review and Jira→coding flows against real services."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

# --- Environment ---
GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.com")
GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
GITLAB_PROJECT_TOKEN = os.environ.get("GITLAB_TOKEN__E2E_ACA_TEST")
GITLAB_PROJECT = os.environ["GITLAB_PROJECT"]
JIRA_URL = os.environ["JIRA_URL"]
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_ISSUE_KEY = os.environ.get("JIRA_ISSUE_KEY")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY")
JIRA_TRIGGER_STATUS = os.environ.get("JIRA_TRIGGER_STATUS", "AI Ready")

REVIEW_BRANCH_NAME = "feature/add-search-endpoint"
_FIXTURES = Path(__file__).parent / "fixtures" / "aca-mr-test"
TEST_ISSUE_SUMMARY = "Add post search endpoint"
TEST_ISSUE_DESCRIPTION = (
    "Add a search endpoint to the blog API so users can find posts by keyword.\n\n"
    "Acceptance Criteria:\n"
    "- Add GET /search?q=<keyword> to the FastAPI app\n"
    "- Search post titles and content for the requested keyword\n"
    "- Return matching posts as JSON\n"
    "- Return 400 when q is empty or only whitespace\n\n"
    "Technical Notes:\n"
    "- Update src/demo_app/main.py to register the new route\n"
    "- Add any helper code needed under src/demo_app/\n"
    "- Keep the implementation consistent with the existing FastAPI app structure"
)


# ---------------------------------------------------------------------------
# GitLab async helpers
# ---------------------------------------------------------------------------


async def _gl_get(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    resp = await client.get(path)
    resp.raise_for_status()
    return resp.json()


async def _gl_paginate(
    client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        p = {**(params or {}), "page": page, "per_page": 100}
        resp = await client.get(path, params=p)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        results.extend(data)
        next_page = resp.headers.get("x-next-page", "")
        if not next_page:
            break
        page = int(next_page)
    return results


def _new_gl_client(url: str, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=url.rstrip("/"),
        headers={"PRIVATE-TOKEN": token},
        timeout=30,
    )


def _new_jira_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=JIRA_URL,
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _poll(
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
        result = await check_fn()
        if result:
            return result
        await asyncio.sleep(interval_s)
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


async def _list_open_coding_mrs(
    gl: httpx.AsyncClient, project_id: int, issue_key: str
) -> list[dict[str, Any]]:
    mrs = await _gl_paginate(
        gl, f"/api/v4/projects/{project_id}/merge_requests", {"state": "opened"}
    )
    return [mr for mr in mrs if _matches_coding_branch(issue_key, mr["source_branch"])]


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


async def create_jira_issue() -> str:
    """Create a fresh Jira issue for the coding-flow portion of the integration test."""
    project_key = _require_jira_project_key()
    async with _new_jira_client() as client:
        resp = await client.post(
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


async def delete_jira_issue(issue_key: str) -> None:
    """Delete a Jira issue created for this integration test run."""
    async with _new_jira_client() as client:
        resp = await client.delete(f"/rest/api/3/issue/{issue_key}")
        resp.raise_for_status()
    log.info("deleted Jira issue", key=issue_key)


# ---------------------------------------------------------------------------
# GitLab state management
# ---------------------------------------------------------------------------


async def cleanup_review_gitlab_state(gl: httpx.AsyncClient, project_id: int) -> None:
    """Close the review MR and delete its branch."""
    log.info("cleaning review GitLab state", project=GITLAB_PROJECT)

    open_mrs = await _gl_paginate(
        gl, f"/api/v4/projects/{project_id}/merge_requests", {"state": "opened"}
    )
    for mr in open_mrs:
        if mr["source_branch"] == REVIEW_BRANCH_NAME:
            resp = await gl.put(
                f"/api/v4/projects/{project_id}/merge_requests/{mr['iid']}",
                json={"state_event": "close"},
            )
            resp.raise_for_status()
            log.info("closed MR", iid=mr["iid"], branch=mr["source_branch"])

    for branch in await _gl_paginate(gl, f"/api/v4/projects/{project_id}/repository/branches"):
        if branch["name"] == REVIEW_BRANCH_NAME:
            encoded = urllib.parse.quote(branch["name"], safe="")
            resp = await gl.delete(f"/api/v4/projects/{project_id}/repository/branches/{encoded}")
            resp.raise_for_status()
            log.info("deleted branch", branch=branch["name"])


async def cleanup_coding_gitlab_state(
    gl: httpx.AsyncClient, project_id: int, issue_key: str
) -> None:
    """Close the coding MR for one Jira issue and delete its branch."""
    log.info("cleaning coding GitLab state", project=GITLAB_PROJECT, issue_key=issue_key)
    coding_prefix = _coding_branch_prefix(issue_key)

    open_mrs = await _gl_paginate(
        gl, f"/api/v4/projects/{project_id}/merge_requests", {"state": "opened"}
    )
    for mr in open_mrs:
        if mr["source_branch"] == coding_prefix or mr["source_branch"].startswith(
            f"{coding_prefix}-"
        ):
            resp = await gl.put(
                f"/api/v4/projects/{project_id}/merge_requests/{mr['iid']}",
                json={"state_event": "close"},
            )
            resp.raise_for_status()
            log.info("closed MR", iid=mr["iid"], branch=mr["source_branch"])

    for branch in await _gl_paginate(gl, f"/api/v4/projects/{project_id}/repository/branches"):
        if branch["name"] == coding_prefix or branch["name"].startswith(f"{coding_prefix}-"):
            encoded = urllib.parse.quote(branch["name"], safe="")
            resp = await gl.delete(f"/api/v4/projects/{project_id}/repository/branches/{encoded}")
            resp.raise_for_status()
            log.info("deleted branch", branch=branch["name"])


async def cleanup_gitlab_state(
    gl: httpx.AsyncClient, project_id: int, issue_key: str | None = None
) -> None:
    """Close test MRs and delete test branches without recreating them."""
    await cleanup_review_gitlab_state(gl, project_id)
    if issue_key is not None:
        await cleanup_coding_gitlab_state(gl, project_id, issue_key)


async def prepare_review_mr(gl: httpx.AsyncClient, project_id: int) -> int:
    """Create a fresh MR with the intentionally buggy review fixture."""
    await cleanup_gitlab_state(gl, project_id)
    log.info("preparing review MR", project=GITLAB_PROJECT, branch=REVIEW_BRANCH_NAME)

    resp = await gl.post(
        f"/api/v4/projects/{project_id}/repository/branches",
        json={"branch": REVIEW_BRANCH_NAME, "ref": "main"},
    )
    resp.raise_for_status()
    log.info("created fresh branch", branch=REVIEW_BRANCH_NAME)

    tree = await _gl_paginate(
        gl,
        f"/api/v4/projects/{project_id}/repository/tree",
        {"ref": REVIEW_BRANCH_NAME, "recursive": "true"},
    )
    existing = {item["path"] for item in tree}

    search_py = (_FIXTURES / "src/demo_app/search.py").read_text()
    main_py = (_FIXTURES / "src/demo_app/main.py").read_text()

    resp = await gl.post(
        f"/api/v4/projects/{project_id}/repository/commits",
        json={
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
        },
    )
    resp.raise_for_status()
    log.info("pushed buggy files to branch")

    resp = await gl.post(
        f"/api/v4/projects/{project_id}/merge_requests",
        json={
            "source_branch": REVIEW_BRANCH_NAME,
            "target_branch": "main",
            "title": "Add post search endpoint",
            "description": (
                "Adds a search endpoint. Has intentional issues for the agent to review."
            ),
        },
    )
    resp.raise_for_status()
    mr = resp.json()
    log.info("created MR", iid=mr["iid"], url=mr["web_url"])
    return mr["iid"]


# ---------------------------------------------------------------------------
# Jira state reset
# ---------------------------------------------------------------------------


async def transition_jira_issue(issue_key: str, target_status: str) -> None:
    """Transition the test issue to the requested Jira status."""
    log.info("transitioning Jira issue", key=issue_key, target_status=target_status)
    async with _new_jira_client() as client:
        resp = await client.get(f"/rest/api/3/issue/{issue_key}/transitions")
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
        resp = await client.post(
            f"/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": transition_id}},
        )
        resp.raise_for_status()
    log.info("Jira issue transitioned", key=issue_key, target_status=target_status)


async def reset_jira_state(issue_key: str) -> None:
    """Transition the test issue back to 'To Do'."""
    await transition_jira_issue(issue_key, "To Do")


async def trigger_jira_issue(issue_key: str) -> None:
    """Move the test issue into the configured trigger status."""
    await transition_jira_issue(issue_key, JIRA_TRIGGER_STATUS)


# ---------------------------------------------------------------------------
# Test: MR review
# ---------------------------------------------------------------------------


async def test_mr_review(gl: httpx.AsyncClient, project_id: int, mr_iid: int) -> None:
    """Verify the agent posts at least one review comment on the MR."""
    log.info("test: MR review", mr_iid=mr_iid)

    async def _has_discussions() -> bool:
        discussions = await _gl_paginate(
            gl, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
        )
        non_system = [d for d in discussions if not d.get("individual_note")]
        if non_system:
            log.info("found review discussions", count=len(non_system))
            return True
        return False

    await _poll("MR review comment appears", _has_discussions, timeout_s=300, interval_s=15)
    log.info("✓ MR review test passed", mr_iid=mr_iid)


# ---------------------------------------------------------------------------
# Test: manual resolution suppression
# ---------------------------------------------------------------------------


async def test_manual_resolution(gl: httpx.AsyncClient, project_id: int, mr_iid: int) -> None:
    """Resolve an agent discussion as a human, trigger re-review, verify no re-raise.

    After the initial review (test_mr_review), this test:
    1. Finds an agent-authored inline discussion and records its topic
    2. Resolves it as the human test user
    3. Pushes a no-op commit to trigger incremental review
    4. Waits for the new review cycle to complete
    5. Verifies no new discussion re-raises the resolved topic
    """
    log.info("test: manual resolution suppression", mr_iid=mr_iid)

    # Find an agent-authored inline discussion to resolve.
    # Skip the *first* inline discussion — test 1c (@mention reply) will use it.
    discussions = await _gl_paginate(
        gl, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
    )
    target_disc: dict[str, Any] | None = None
    resolved_topic = ""
    skipped_first = False
    for d in discussions:
        if d.get("individual_note"):
            continue
        notes = d.get("notes", [])
        first_note = notes[0] if notes else {}
        if first_note.get("position") and not first_note.get("system"):
            if not skipped_first:
                skipped_first = True
                continue
            resolved_topic = first_note.get("body", "")[:80]
            target_disc = d
            break

    if target_disc is None:
        log.warning("no inline agent discussion found — skipping manual resolution test")
        return

    disc_id: str = target_disc["id"]
    log.info("resolving discussion as human", discussion_id=disc_id, topic=resolved_topic)

    # Resolve the discussion (acts as the human GITLAB_TOKEN user)
    resp = await gl.put(
        f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions/{disc_id}",
        json={"resolved": True},
    )
    resp.raise_for_status()

    # Record discussion count before re-review (both inline threads and summary notes)
    pre_review_discussions = await _gl_paginate(
        gl, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
    )
    pre_total_discussions = len(pre_review_discussions)
    pre_total_notes = sum(len(d.get("notes", [])) for d in pre_review_discussions)
    log.info(
        "pre-review discussion count",
        total_discussions=pre_total_discussions,
        total_notes=pre_total_notes,
    )

    # Push a no-op commit to trigger incremental review
    resp = await gl.post(
        f"/api/v4/projects/{project_id}/repository/commits",
        json={
            "branch": REVIEW_BRANCH_NAME,
            "commit_message": "chore: trigger re-review for resolution test",
            "actions": [
                {
                    "action": "update",
                    "file_path": "src/demo_app/search.py",
                    "content": (_FIXTURES / "src/demo_app/search.py").read_text()
                    + "\n# re-review trigger\n",
                },
            ],
        },
    )
    resp.raise_for_status()
    log.info("pushed no-op commit to trigger re-review")

    # Poll for new review activity
    async def _new_review_cycle() -> bool:
        discs = await _gl_paginate(
            gl, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
        )
        new_total = len(discs)
        new_notes = sum(len(d.get("notes", [])) for d in discs)
        if new_total > pre_total_discussions or new_notes > pre_total_notes:
            log.info(
                "new review cycle detected",
                discussions=new_total,
                notes=new_notes,
                prev_discussions=pre_total_discussions,
                prev_notes=pre_total_notes,
            )
            return True
        return False

    await _poll("re-review cycle completes", _new_review_cycle, timeout_s=300, interval_s=15)

    # Verify the resolved topic was not re-raised at the same file+line.
    fresh_discussions = await _gl_paginate(
        gl, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
    )

    resolved_notes = target_disc.get("notes", [])
    resolved_position = resolved_notes[0].get("position", {}) if resolved_notes else {}
    resolved_file = resolved_position.get("new_path", "")
    resolved_line = resolved_position.get("new_line")
    log.info(
        "checking for re-raise at resolved location",
        file=resolved_file,
        line=resolved_line,
    )

    re_raised = False
    for d in fresh_discussions:
        if d.get("individual_note") or d["id"] == disc_id:
            continue
        notes = d.get("notes", [])
        if not notes:
            continue
        first_note = notes[0]
        pos = first_note.get("position") or {}
        if pos.get("new_path") == resolved_file and pos.get("new_line") == resolved_line:
            log.warning(
                "re-raise detected at same location",
                discussion_id=d["id"],
                file=resolved_file,
                line=resolved_line,
                body_preview=first_note.get("body", "")[:100],
            )
            re_raised = True

    if re_raised:
        raise AssertionError(
            f"Agent re-raised resolved discussion at {resolved_file}:{resolved_line}"
        )

    log.info("✓ manual resolution test passed — resolved topic not re-raised", mr_iid=mr_iid)


async def _resolve_agent_username() -> str | None:
    """Discover the agent's bot username via GET /user with the project token."""
    if not GITLAB_PROJECT_TOKEN:
        return None
    async with _new_gl_client(GITLAB_URL, GITLAB_PROJECT_TOKEN) as agent_gl:
        user = await _gl_get(agent_gl, "/api/v4/user")
    username: str = user["username"]
    log.info("resolved agent username", username=username)
    return username


async def test_mention_reply(
    gl: httpx.AsyncClient, project_id: int, mr_iid: int, agent_username: str
) -> None:
    """Post an @mention on an existing review thread and verify the agent replies."""
    log.info("test: @mention reply", mr_iid=mr_iid, agent_username=agent_username)

    # Find a review discussion to reply in
    discussions = await _gl_paginate(
        gl, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
    )
    review_disc: dict[str, Any] | None = None
    for d in discussions:
        if not d.get("individual_note"):
            review_disc = d
            break
    if review_disc is None:
        raise RuntimeError("No review discussion found to reply in")

    disc_id: str = review_disc["id"]
    initial_note_count = len(review_disc.get("notes", []))
    log.info("posting @mention", discussion_id=disc_id, note_count=initial_note_count)

    # Post @mention in the thread
    resp = await gl.post(
        f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions/{disc_id}/notes",
        json={"body": f"@{agent_username} can you explain this in more detail?"},
    )
    resp.raise_for_status()

    # Poll for the agent's reply
    async def _has_reply() -> bool:
        d = await _gl_get(
            gl, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions/{disc_id}"
        )
        notes = d.get("notes", [])
        if len(notes) > initial_note_count + 1:
            log.info("agent replied", note_count=len(notes))
            return True
        return False

    await _poll("agent replies to @mention", _has_reply, timeout_s=300, interval_s=15)
    log.info("✓ @mention reply test passed", mr_iid=mr_iid)


# ---------------------------------------------------------------------------
# Test: Jira → coding flow
# ---------------------------------------------------------------------------


async def test_jira_coding_flow(
    gl: httpx.AsyncClient,
    project_id: int,
    issue_key: str,
    existing_mr_ids: set[int],
) -> None:
    """Verify the agent picks up the Jira issue and eventually creates an MR."""
    log.info("test: Jira→coding flow", issue=issue_key)

    async with _new_jira_client() as jira:

        async def _issue_status() -> str:
            resp = await jira.get(f"/rest/api/3/issue/{issue_key}")
            resp.raise_for_status()
            return resp.json()["fields"]["status"]["name"]

        async def _reached_in_progress() -> bool:
            status = await _issue_status()
            log.info("Jira issue status", status=status)
            return status in ("In Progress", "Done")

        async def _reached_done() -> bool:
            status = await _issue_status()
            log.info("Jira issue status", status=status)
            return status == "Done"

        await _poll("Jira issue reaches In Progress", _reached_in_progress, timeout_s=300)
        await _poll("Jira issue reaches Done", _reached_done, timeout_s=480)

    async def _new_coding_mr() -> dict[str, Any] | None:
        for mr in await _list_open_coding_mrs(gl, project_id, issue_key):
            if mr["iid"] not in existing_mr_ids:
                log.info("found coding MR", iid=mr["iid"], branch=mr["source_branch"])
                return mr
        return None

    mr = await _poll("GitLab coding MR appears", _new_coding_mr, timeout_s=300)
    log.info("✓ Jira coding flow test passed", mr_iid=mr["iid"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    log.info(
        "starting ACA integration tests",
        project=GITLAB_PROJECT,
        jira_issue=JIRA_ISSUE_KEY,
        jira_project=_require_jira_project_key(),
    )

    async with _new_gl_client(GITLAB_URL, GITLAB_TOKEN) as gl:
        encoded_project = urllib.parse.quote(GITLAB_PROJECT, safe="")
        project = await _gl_get(gl, f"/api/v4/projects/{encoded_project}")
        project_id: int = project["id"]

        failures: list[str] = []
        created_jira_issue = False

        # --- Test 1: MR review ---
        mr_iid: int | None = None
        try:
            mr_iid = await prepare_review_mr(gl, project_id)
            await test_mr_review(gl, project_id, mr_iid)
        except Exception as exc:
            log.error("MR review test failed", error=str(exc))
            failures.append(f"MR review: {exc}")

        # --- Test 1b: manual resolution suppression ---
        if mr_iid is not None:
            try:
                await test_manual_resolution(gl, project_id, mr_iid)
            except Exception as exc:
                log.error("manual resolution test failed", error=str(exc))
                failures.append(f"Manual resolution: {exc}")

        # --- Test 1c: @mention reply ---
        if mr_iid is not None:
            agent_username = await _resolve_agent_username()
            if agent_username:
                try:
                    await test_mention_reply(gl, project_id, mr_iid, agent_username)
                except Exception as exc:
                    log.error("@mention reply test failed", error=str(exc))
                    failures.append(f"@mention reply: {exc}")
            else:
                log.warning("skipping @mention test — no project token configured")

        # --- Test 2: Jira → coding ---
        jira_issue_key = JIRA_ISSUE_KEY
        try:
            if jira_issue_key is None:
                jira_issue_key = await create_jira_issue()
                created_jira_issue = True
            else:
                await reset_jira_state(jira_issue_key)
            await cleanup_gitlab_state(gl, project_id, jira_issue_key)
            existing_mr_ids = {
                mr["iid"] for mr in await _list_open_coding_mrs(gl, project_id, jira_issue_key)
            }
            await trigger_jira_issue(jira_issue_key)
            await test_jira_coding_flow(gl, project_id, jira_issue_key, existing_mr_ids)
        except Exception as exc:
            log.error("Jira coding flow test failed", error=str(exc))
            failures.append(f"Jira coding flow: {exc}")

        # --- Cleanup ---
        try:
            await cleanup_gitlab_state(gl, project_id, jira_issue_key)
            if JIRA_ISSUE_KEY:
                await reset_jira_state(JIRA_ISSUE_KEY)
            elif created_jira_issue and jira_issue_key is not None:
                await delete_jira_issue(jira_issue_key)
        except Exception as exc:
            log.warning("cleanup failed (non-fatal)", error=str(exc))

    if failures:
        log.error("integration tests FAILED", failures=failures)
        sys.exit(1)

    log.info("✓ all ACA integration tests passed")


if __name__ == "__main__":
    asyncio.run(main())
