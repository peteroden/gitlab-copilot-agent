#!/usr/bin/env python3
"""Reset the demo MR — close existing, delete branch, recreate with buggy code."""

from __future__ import annotations

import os
import sys

import gitlab
import structlog

log = structlog.get_logger()


def main() -> None:
    gl_url = os.environ.get("GITLAB_URL", "https://gitlab.com")
    gl_token = os.environ.get("GITLAB_TOKEN")
    if not gl_token:
        print("Error: GITLAB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    project_path = sys.argv[1] if len(sys.argv) > 1 else "peteroden/copilot-demo"

    gl = gitlab.Gitlab(gl_url, private_token=gl_token)
    gl.auth()
    project = gl.projects.get(project_path)

    branch_name = "feature/add-search-endpoint"

    # Close existing MRs on that branch
    for mr in project.mergerequests.list(state="opened", get_all=True):
        if mr.source_branch == branch_name:
            mr.state_event = "close"
            mr.save()
            print(f"Closed MR !{mr.iid}")

    # Delete the branch
    try:
        project.branches.delete(branch_name)
        print(f"Deleted branch {branch_name}")
    except Exception:
        print(f"Branch {branch_name} not found (already deleted)")

    # Recreate branch from main
    project.branches.create({"branch": branch_name, "ref": "main"})
    print(f"Created fresh branch {branch_name}")

    # Check existing files
    tree = project.repository_tree(ref=branch_name, recursive=True, get_all=True)
    existing = {item["path"] for item in tree}

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

    project.commits.create(
        {
            "branch": branch_name,
            "commit_message": "Add search endpoint with keyword matching",
            "actions": actions,
        }
    )
    print("Pushed buggy files to branch")

    mr = project.mergerequests.create(
        {
            "source_branch": branch_name,
            "target_branch": "main",
            "title": "Add post search endpoint",
            "description": (
                "Adds a search endpoint to find posts by keyword.\n\n"
                "This MR has intentional issues for the agent to review."
            ),
        }
    )
    print(f"✅ Created MR !{mr.iid}: {mr.web_url}")


if __name__ == "__main__":
    main()
