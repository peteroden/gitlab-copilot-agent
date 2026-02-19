"""Mock GitLab API + dumb git HTTP server for E2E tests.

Serves two things on one port:
  /api/v4/...       — GitLab REST API responses (MR details, changes, discussions)
  /repo.git/...     — bare git repo via dumb HTTP protocol (for git clone)

Tracks received discussion posts in /discussions for test assertions.

Usage: uv run python tests/e2e/mock_gitlab.py [--port 9999]
"""

import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response

app = FastAPI()

# Recorded state for test assertions
discussions: list[dict] = []
merge_requests: list[dict] = []
pushes: list[dict] = []

# Bare git repo created at startup
_bare_repo: Path | None = None
_head_sha: str = ""


def _pkt_line(s: str) -> bytes:
    data = s.encode()
    return f"{len(data) + 4:04x}".encode() + data


PKT_FLUSH = b"0000"

PROJECT_ID = 999
MR_IID = 1
BASE_SHA = "aaa0000000000000000000000000000000000000"
START_SHA = "bbb0000000000000000000000000000000000000"
HEAD_SHA = "ccc0000000000000000000000000000000000000"

SAMPLE_FILE = "app.py"
SAMPLE_DIFF = """\
@@ -1,3 +1,5 @@
+import os
+
 def main():
-    pass
+    print("hello")
     return 0
"""


@app.get("/api/v4/projects/{project_id}")
async def get_project(project_id: int) -> dict:
    return {"id": project_id, "path_with_namespace": "test/e2e-repo"}


@app.get("/api/v4/projects/{project_id}/merge_requests/{mr_iid}")
async def get_mr(project_id: int, mr_iid: int) -> dict:
    return {
        "iid": mr_iid,
        "title": "E2E test MR",
        "description": "Automated E2E test merge request",
        "state": "opened",
        "source_branch": "feature",
        "target_branch": "main",
        "diff_refs": {
            "base_sha": BASE_SHA,
            "start_sha": START_SHA,
            "head_sha": HEAD_SHA,
        },
        "changes": [
            {
                "old_path": SAMPLE_FILE,
                "new_path": SAMPLE_FILE,
                "diff": SAMPLE_DIFF,
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
            }
        ],
    }


@app.get("/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes")
async def get_mr_changes(project_id: int, mr_iid: int) -> dict:
    mr = await get_mr(project_id, mr_iid)
    return mr


@app.post("/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions")
async def create_discussion(project_id: int, mr_iid: int, request: Request) -> dict:
    body = await request.json()
    discussions.append(body)
    return {"id": "d1", "notes": [{"id": 1, "body": body.get("body", "")}]}


@app.post("/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes")
async def create_note(project_id: int, mr_iid: int, request: Request) -> dict:
    body = await request.json()
    discussions.append({"_type": "note", **body})
    return {"id": 1, "body": body.get("body", "")}


@app.post("/api/v4/projects/{project_id}/merge_requests")
async def create_mr(project_id: int, request: Request) -> dict:
    body = await request.json()
    iid = len(merge_requests) + 2
    merge_requests.append({"project_id": project_id, "iid": iid, **body})
    return {"id": iid, "iid": iid, "web_url": f"http://mock/mr/{iid}"}


@app.get("/discussions")
async def get_discussions() -> list[dict]:
    """Test assertion endpoint — returns all recorded discussions/notes."""
    return discussions


@app.delete("/discussions")
async def clear_discussions() -> dict:
    """Reset recorded discussions between test runs."""
    discussions.clear()
    return {"cleared": True}


@app.get("/merge_requests")
async def get_recorded_mrs() -> list[dict]:
    return merge_requests


@app.delete("/merge_requests")
async def clear_mrs() -> dict:
    merge_requests.clear()
    return {"cleared": True}


@app.get("/pushes")
async def get_pushes() -> list[dict]:
    return pushes


@app.delete("/pushes")
async def clear_pushes() -> dict:
    pushes.clear()
    return {"cleared": True}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _create_bare_repo() -> Path:
    """Create a minimal bare git repo with one file for clone tests."""
    tmp = Path(tempfile.mkdtemp(prefix="e2e-repo-"))
    work = tmp / "work"
    work.mkdir()
    (work / SAMPLE_FILE).write_text('import os\n\ndef main():\n    print("hello")\n    return 0\n')
    subprocess.run(["git", "init", "-b", "main"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    import os as _os

    env = {
        **_os.environ,
        "GIT_COMMITTER_NAME": "E2E",
        "GIT_COMMITTER_EMAIL": "e2e@test",
    }
    subprocess.run(
        ["git", "commit", "-m", "init", "--author", "E2E <e2e@test>"],
        cwd=work,
        check=True,
        capture_output=True,
        env=env,
    )
    bare = tmp / "repo.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "update-server-info"], cwd=bare, check=True, capture_output=True)
    return bare


@app.on_event("startup")
async def startup() -> None:
    global _bare_repo, _head_sha  # noqa: PLW0603
    _bare_repo = _create_bare_repo()
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_bare_repo, capture_output=True, text=True, check=True
    )
    _head_sha = result.stdout.strip()


@app.get("/repo.git/{path:path}")
async def serve_git(path: str, request: Request) -> Response:
    """Serve bare git repo files. Supports smart HTTP push protocol."""
    if path == "info/refs" and request.query_params.get("service") == "git-receive-pack":
        body = (
            _pkt_line("# service=git-receive-pack\n")
            + PKT_FLUSH
            + _pkt_line(f"{_head_sha} refs/heads/main\0 report-status\n")
            + PKT_FLUSH
        )
        return Response(content=body, media_type="application/x-git-receive-pack-advertisement")
    assert _bare_repo is not None
    file_path = _bare_repo / path
    if not file_path.is_file():
        return Response(status_code=404)
    return FileResponse(file_path)


@app.post("/repo.git/git-receive-pack")
async def git_receive_pack(request: Request) -> Response:
    """Accept git push — record the ref and return success."""
    body = await request.body()
    ref = "unknown"
    try:
        pkt_len = int(body[:4], 16)
        first_line = body[4:pkt_len]
        parts = first_line.split(b"\x00")[0].split()
        if len(parts) >= 3:
            ref = parts[2].decode()
    except Exception:
        pass
    pushes.append({"ref": ref})
    resp = _pkt_line("unpack ok\n") + _pkt_line(f"ok {ref}\n") + PKT_FLUSH
    return Response(content=resp, media_type="application/x-git-receive-pack-result")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9999)
