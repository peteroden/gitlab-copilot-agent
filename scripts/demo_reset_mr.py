#!/usr/bin/env python3
"""Reset the demo MR — close existing, delete branch, recreate with buggy code."""

from __future__ import annotations

import os
import sys
from urllib.parse import quote

import httpx
import structlog

log = structlog.get_logger()


def _get_all_pages(client: httpx.Client, path: str, **params: str) -> list[dict]:
    """Fetch all pages of a paginated GitLab API endpoint."""
    results: list[dict] = []
    page = 1
    while True:
        resp = client.get(path, params={**params, "page": str(page), "per_page": "100"})
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        results.extend(data)
        page += 1
    return results


def main() -> None:
    """Close existing demo MR, delete branch, and recreate with buggy code."""
    gl_url = os.environ.get("GITLAB_URL", "https://gitlab.com")
    gl_token = os.environ.get("GITLAB_TOKEN")
    if not gl_token:
        print("Error: GITLAB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    project_path = sys.argv[1] if len(sys.argv) > 1 else "peteroden/copilot-demo"
    encoded_path = quote(project_path, safe="")

    gl = httpx.Client(
        base_url=f"{gl_url}/api/v4",
        headers={"PRIVATE-TOKEN": gl_token},
    )

    # Verify authentication
    auth_resp = gl.get("/user")
    auth_resp.raise_for_status()

    # Verify project access
    proj_resp = gl.get(f"/projects/{encoded_path}")
    proj_resp.raise_for_status()

    branch_name = "feature/add-search-endpoint"
    encoded_branch = quote(branch_name, safe="")

    # Close existing MRs on that branch
    open_mrs = _get_all_pages(gl, f"/projects/{encoded_path}/merge_requests", state="opened")
    for mr in open_mrs:
        if mr["source_branch"] == branch_name:
            close_resp = gl.put(
                f"/projects/{encoded_path}/merge_requests/{mr['iid']}",
                json={"state_event": "close"},
            )
            close_resp.raise_for_status()
            print(f"Closed MR !{mr['iid']}")

    # Delete the branch
    del_resp = gl.delete(f"/projects/{encoded_path}/repository/branches/{encoded_branch}")
    if del_resp.status_code < 300:
        print(f"Deleted branch {branch_name}")
    else:
        print(f"Branch {branch_name} not found (already deleted)")

    # Recreate branch from main
    create_resp = gl.post(
        f"/projects/{encoded_path}/repository/branches",
        params={"branch": branch_name, "ref": "main"},
    )
    create_resp.raise_for_status()
    print(f"Created fresh branch {branch_name}")

    # Check existing files
    tree_items = _get_all_pages(
        gl,
        f"/projects/{encoded_path}/repository/tree",
        ref=branch_name,
        recursive="true",
    )
    existing = {item["path"] for item in tree_items}

    search_py = '''\
"""Search functionality for the Blog Post API."""

from demo_app.database import _get_connection


def search_posts(query):
    conn = _get_connection()
    results = conn.execute(
        f"SELECT * FROM posts WHERE title LIKE '%{query}%'"
        f" OR content LIKE '%{query}%'"
    ).fetchall()
    conn.close()
    return [dict(row) for row in results]


def search_by_date(start, end):
    conn = _get_connection()
    results = conn.execute(
        f"SELECT * FROM posts WHERE created_at BETWEEN '{start}' AND '{end}'"
    ).fetchall()
    conn.close()
    return results
'''

    main_py = '''\
"""Blog Post API — main application."""

from fastapi import FastAPI, HTTPException

from demo_app import database
from demo_app.auth import verify_api_key
from demo_app.search import search_posts

app = FastAPI(title="Blog Post API")


@app.get("/posts/{post_id}")
def get_post(post_id: str):
    try:
        post = database.get_post(post_id)
        if not post:
            raise HTTPException(status_code=404)
        return post
    except Exception as e:
        print(f"Error: {e}")
        raise


@app.get("/posts")
def list_posts(author: str | None = None):
    if author:
        return database.get_posts_by_author(author)
    return database.get_posts_by_author("%")


@app.get("/search")
def search(q: str):
    return search_posts(q)


@app.post("/posts")
def create_post(title: str, content: str, author: str, api_key: str):
    if not verify_api_key(api_key):
        raise HTTPException(status_code=401)
    return database.create_post(title, content, author)
'''

    actions = [
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
    ]

    commit_resp = gl.post(
        f"/projects/{encoded_path}/repository/commits",
        json={
            "branch": branch_name,
            "commit_message": "Add search endpoint with keyword matching",
            "actions": actions,
        },
    )
    commit_resp.raise_for_status()
    print("Pushed buggy files to branch")

    mr_resp = gl.post(
        f"/projects/{encoded_path}/merge_requests",
        json={
            "source_branch": branch_name,
            "target_branch": "main",
            "title": "Add post search endpoint",
            "description": (
                "Adds a search endpoint to find posts by keyword.\n\n"
                "This MR has intentional issues for the agent to review."
            ),
        },
    )
    mr_resp.raise_for_status()
    mr_data = mr_resp.json()
    print(f"✅ Created MR !{mr_data['iid']}: {mr_data['web_url']}")

    gl.close()


if __name__ == "__main__":
    main()
