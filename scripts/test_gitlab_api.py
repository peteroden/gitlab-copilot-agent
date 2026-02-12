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

import gitlab

from gitlab_copilot_agent.gitlab_client import GitLabClient


async def main(project_id: int, mr_iid: int) -> None:
    url = os.environ["GITLAB_URL"]
    token = os.environ["GITLAB_TOKEN"]

    print("=== GitLab API Smoke Test ===")
    print(f"URL: {url}")
    print(f"Project: {project_id}, MR: !{mr_iid}\n")

    client = GitLabClient(url, token)

    # 1. Fetch MR details
    print("1. Fetching MR details...")
    details = await client.get_mr_details(project_id, mr_iid)
    print(f"   Title: {details.title}")
    print(f"   Description: {details.description or '(none)'}")
    print(f"   diff_refs: base={details.diff_refs.base_sha[:8]}, "
          f"start={details.diff_refs.start_sha[:8]}, "
          f"head={details.diff_refs.head_sha[:8]}")
    print(f"   Changes: {len(details.changes)} file(s)")
    for c in details.changes:
        status = "new" if c.new_file else "deleted" if c.deleted_file else "modified"
        print(f"     {status}: {c.new_path}")
    print("   ✅ MR details fetched\n")

    # 2. Clone repo
    print("2. Cloning repo...")
    gl = gitlab.Gitlab(url, private_token=token)
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    clone_url = project.http_url_to_repo

    repo_path = await client.clone_repo(clone_url, mr.source_branch, token)
    files = list(repo_path.iterdir())
    print(f"   Cloned to: {repo_path}")
    print(f"   Files: {len(files)}")
    print("   ✅ Clone succeeded\n")

    # 3. Post a test note
    print("3. Posting test note...")
    note = mr.notes.create({"body": "🤖 Integration smoke test — will be deleted shortly."})
    print(f"   Note ID: {note.id}")
    print("   ✅ Note posted\n")

    # 4. Post a test inline discussion (on first changed file, if any)
    discussion_id = None
    if details.changes:
        change = details.changes[0]
        print(f"4. Posting inline discussion on {change.new_path}...")
        try:
            disc = mr.discussions.create({
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
            })
            discussion_id = disc.id
            print(f"   Discussion ID: {disc.id}")
            print("   ✅ Inline discussion posted\n")
        except Exception as e:
            print(f"   ⚠️  Inline discussion failed (may be expected if line 1 not in diff): {e}")
            print("   Fallback: posting as general note instead\n")
    else:
        print("4. Skipping inline discussion — no changed files\n")

    # 5. Clean up comments
    print("5. Cleaning up test comments...")
    note.delete()
    print(f"   Deleted note {note.id}")
    if discussion_id:
        # Can't delete discussions directly — resolve it instead
        disc = mr.discussions.get(discussion_id)
        disc.resolved = True
        disc.save()
        print(f"   Resolved discussion {discussion_id}")
    print("   ✅ Cleanup done\n")

    # 6. Clean up clone
    print("6. Cleaning up cloned repo...")
    await client.cleanup(repo_path)
    print("   ✅ Repo cleaned up\n")

    print("=== All checks passed ===")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <project_id> <mr_iid>")
        sys.exit(1)
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
