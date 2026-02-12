"""Tests for the GitLab client."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitlab_copilot_agent.gitlab_client import GitLabClient, MRChange, MRDetails, MRDiffRef


@pytest.fixture
def mock_gl(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    mr_mock = MagicMock()
    mr_mock.changes.return_value = {
        "title": "Test MR",
        "description": "A test merge request",
        "diff_refs": {
            "base_sha": "aaa111",
            "start_sha": "ccc333",
            "head_sha": "bbb222",
        },
        "changes": [
            {
                "old_path": "src/main.py",
                "new_path": "src/main.py",
                "diff": "@@ -1,3 +1,4 @@\n+new line\n",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
            }
        ],
    }
    mock.projects.get.return_value.mergerequests.get.return_value = mr_mock
    monkeypatch.setattr("gitlab_copilot_agent.gitlab_client.gitlab.Gitlab", lambda *a, **kw: mock)
    return mock


async def test_get_mr_details(mock_gl: MagicMock) -> None:
    client = GitLabClient("https://gitlab.com", "token")
    details = await client.get_mr_details(42, 7)

    assert isinstance(details, MRDetails)
    assert details.title == "Test MR"
    assert details.description == "A test merge request"
    assert details.diff_refs == MRDiffRef(
        base_sha="aaa111", start_sha="ccc333", head_sha="bbb222"
    )
    assert len(details.changes) == 1
    assert details.changes[0] == MRChange(
        old_path="src/main.py",
        new_path="src/main.py",
        diff="@@ -1,3 +1,4 @@\n+new line\n",
    )


async def _run(*cmd: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()


async def test_clone_repo_success(tmp_path: Path) -> None:
    client = GitLabClient("https://gitlab.com", "token")

    # Create a bare git repo to clone from
    bare = tmp_path / "bare.git"
    bare.mkdir()
    await _run("git", "init", "--bare", "--initial-branch=main", str(bare))
    await _run("git", "clone", str(bare), str(tmp_path / "work"))

    work = tmp_path / "work"
    (work / "file.txt").write_text("hello")
    await _run("git", "-C", str(work), "add", ".")
    await _run(
        "git", "-C", str(work),
        "-c", "user.name=Test", "-c", "user.email=t@t.com",
        "commit", "-m", "init",
    )
    await _run("git", "-C", str(work), "push")

    # Clone via our client (no auth needed for local path)
    cloned = await client.clone_repo(str(bare), "main", "fake-token")
    try:
        assert cloned.exists()
        assert (cloned / "file.txt").read_text() == "hello"
    finally:
        await client.cleanup(cloned)
        assert not cloned.exists()
