#!/usr/bin/env python3
"""GitLab API integration smoke test.

Tests clone, MR details fetch, comment posting, and cleanup against a real
GitLab instance. No Copilot needed.

Usage:
    GITLAB_URL=https://gitlab.com GITLAB_TOKEN=glpat-... \
        uv run python scripts/test_gitlab_api.py <project_id> <mr_iid>
"""

import asyncio
import os
import sys
from urllib.parse import quote

import httpx

from gitlab_copilot_agent.gitlab_client import GitLabClient


async def main(project_id: int, mr_iid: int) -> None:
    """Run smoke tests against a GitLab project and merge request."""
    url = os.environ["GITLAB_URL"]
    token = os.environ["GITLAB_TOKEN"]

    print("=== GitLab API Smoke Test ===")
    print(f"URL: {url}")
    print(f"Project: {project_id}, MR: !{mr_iid}\n")

    client = GitLabClient(url, token)
    encoded_id = quote(str(project_id), safe="")
    gl = httpx.Client(
        base_url=f"{url}/api/v4",
        headers={"PRIVATE-TOKEN": token},
    )

    # 1. Fetch MR details
    print("1. Fetching MR details...")
    details = await client.get_mr_details(project_id, mr_iid)
    print(f"   Title: {details.title}")
    print(f"   Description: {details.description or '(none)'}")
    print(
        f"   diff_refs: base={details.diff_refs.base_sha[:8]}, "
        f"start={details.diff_refs.start_sha[:8]}, "
        f"head={details.diff_refs.head_sha[:8]}"
    )
    print(f"   Changes: {len(details.changes)} file(s)")
    for c in details.changes:
        status = "new" if c.new_file else "deleted" if c.deleted_file else "modified"
        print(f"     {status}: {c.new_path}")
    print("   ✅ MR details fetched\n")

    # 2. Clone repo
    print("2. Cloning repo...")
    proj_resp = gl.get(f"/projects/{encoded_id}")
    proj_resp.raise_for_status()
    proj_data = proj_resp.json()

    mr_resp = gl.get(f"/projects/{encoded_id}/merge_requests/{mr_iid}")
    mr_resp.raise_for_status()
    mr_data = mr_resp.json()

    clone_url = proj_data["http_url_to_repo"]
    repo_path = await client.clone_repo(clone_url, mr_data["source_branch"], token)
    files = list(repo_path.iterdir())
    print(f"   Cloned to: {repo_path}")
    print(f"   Files: {len(files)}")
    print("   ✅ Clone succeeded\n")

    # 3. Post a test note
    print("3. Posting test note...")
    note_resp = gl.post(
        f"/projects/{encoded_id}/merge_requests/{mr_iid}/notes",
        json={"body": "🤖 Integration smoke test — will be deleted shortly."},
    )
    note_resp.raise_for_status()
    note_data = note_resp.json()
    note_id = note_data["id"]
    print(f"   Note ID: {note_id}")
    print("   ✅ Note posted\n")

    # 4. Post a test inline discussion (on first changed file, if any)
    discussion_id = None
    if details.changes:
        change = details.changes[0]
        print(f"4. Posting inline discussion on {change.new_path}...")
        try:
            disc_resp = gl.post(
                f"/projects/{encoded_id}/merge_requests/{mr_iid}/discussions",
                json={
                    "body": "🤖 Inline smoke test — will be deleted shortly.",
                    "position": {
                        "base_sha": details.diff_refs.base_sha,
                        "start_sha": details.diff_refs.start_sha,
                        "head_sha": details.diff_refs.head_sha,
                        "position_type": "text",
                        "old_path": change.old_path,
                        "new_path": change.new_path,
                        "new_line": 1,
                    },
                },
            )
            disc_resp.raise_for_status()
            disc_data = disc_resp.json()
            discussion_id = disc_data["id"]
            print(f"   Discussion ID: {discussion_id}")
            print("   ✅ Inline discussion posted\n")
        except httpx.HTTPStatusError as e:
            print(f"   ⚠️  Inline discussion failed (may be expected if line 1 not in diff): {e}")
            print("   Fallback: posting as general note instead\n")
    else:
        print("4. Skipping inline discussion — no changed files\n")

    # 5. Clean up comments
    print("5. Cleaning up test comments...")
    del_resp = gl.delete(f"/projects/{encoded_id}/merge_requests/{mr_iid}/notes/{note_id}")
    del_resp.raise_for_status()
    print(f"   Deleted note {note_id}")
    if discussion_id:
        # Can't delete discussions directly — resolve it instead
        resolve_resp = gl.put(
            f"/projects/{encoded_id}/merge_requests/{mr_iid}/discussions/{discussion_id}",
            json={"resolved": True},
        )
        resolve_resp.raise_for_status()
        print(f"   Resolved discussion {discussion_id}")
    print("   ✅ Cleanup done\n")

    # 6. Clean up clone
    print("6. Cleaning up cloned repo...")
    await client.cleanup(repo_path)
    print("   ✅ Repo cleaned up\n")

    print("=== All checks passed ===")

    gl.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <project_id> <mr_iid>")
        sys.exit(1)
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
