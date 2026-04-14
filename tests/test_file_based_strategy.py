"""Tests for the file-based prompt strategy (Phase 8.1b)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from gitlab_copilot_agent.coding_engine import build_jira_coding_prompt
from gitlab_copilot_agent.coding_pipeline import CodingContext, CodingPipeline
from gitlab_copilot_agent.discussion_engine import build_discussion_prompt
from gitlab_copilot_agent.discussion_models import (
    AgentIdentity,
    Discussion,
    DiscussionHistory,
    DiscussionNote,
)
from gitlab_copilot_agent.discussion_pipeline import (
    DiscussionContext,
    DiscussionPipeline,
    _format_current_thread,
    _format_other_discussions,
)
from gitlab_copilot_agent.gitlab_client import MRChange, MRDetails, MRDiffRef
from gitlab_copilot_agent.jira_models import (
    JiraIssue,
    JiraIssueFields,
    JiraStatus,
)
from gitlab_copilot_agent.pipeline import _context_dir_for, write_context_file
from gitlab_copilot_agent.review_engine import ReviewRequest, build_review_prompt
from gitlab_copilot_agent.review_pipeline import ReviewContext, ReviewPipeline
from tests.conftest import (
    JIRA_SETTINGS,
    make_mock_gitlab_client,
    make_resolved_project,
    make_settings,
    make_task_event,
)

# -- Constants --
AGENT_USER_ID = 99
AGENT_USERNAME = "review-bot"
MR_TITLE = "Add user authentication"
MR_DESCRIPTION = "Implements JWT-based auth with refresh token support"
BASE_SHA = "abc123def456"
DISCUSSION_ID = "disc-fb-001"
NOTE_BODY = "Consider adding input validation here."
NOTE_ID = 501
SOURCE_BRANCH = "feature/auth"
TARGET_BRANCH = "main"
PROJECT_PATH = "group/project"
DIFF_REFS = MRDiffRef(base_sha=BASE_SHA, start_sha="sss111", head_sha="hhh222")
SAMPLE_DIFF = "@@ -1,3 +1,5 @@\n+import jwt\n+\n def login():\n     pass"
DIFF_TEXT = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@"
FILE_BASED_MAX = 2000
_REVIEW_EXEC = "gitlab_copilot_agent.review_pipeline.asyncio.create_subprocess_exec"
_DISC_EXEC = "gitlab_copilot_agent.discussion_pipeline.asyncio.create_subprocess_exec"


# -- Factories --
def _mr_details(**ov: Any) -> MRDetails:
    return MRDetails(
        **(
            {
                "title": MR_TITLE,
                "description": MR_DESCRIPTION,
                "diff_refs": DIFF_REFS,
                "changes": [
                    MRChange(
                        old_path="src/auth.py",
                        new_path="src/auth.py",
                        diff=SAMPLE_DIFF,
                    )
                ],
            }
            | ov
        ),
    )


def _review_req(**ov: Any) -> ReviewRequest:
    return ReviewRequest(
        **(
            {
                "title": MR_TITLE,
                "description": MR_DESCRIPTION,
                "source_branch": SOURCE_BRANCH,
                "target_branch": TARGET_BRANCH,
                "commit_messages": ["feat: add JWT auth"],
            }
            | ov
        ),
    )


def _note(
    note_id: int = 1,
    author_id: int = AGENT_USER_ID,
    body: str = NOTE_BODY,
    **kw: Any,
) -> DiscussionNote:
    return DiscussionNote(
        note_id=note_id,
        author_id=author_id,
        body=body,
        **(
            {
                "author_username": AGENT_USERNAME,
                "created_at": "2024-01-15T10:00:00Z",
                "is_system": False,
                "resolved": None,
                "resolvable": True,
                "position": None,
            }
            | kw
        ),
    )


def _disc(
    discussion_id: str = DISCUSSION_ID,
    notes: list[DiscussionNote] | None = None,
    **kw: Any,
) -> Discussion:
    return Discussion(
        discussion_id=discussion_id,
        notes=notes or [_note()],
        **({"is_resolved": False, "is_inline": True} | kw),
    )


def _discussion_prompt(
    strategy: str = "file-based",
    base_sha: str | None = BASE_SHA,
) -> str:
    agent = AgentIdentity(user_id=AGENT_USER_ID, username=AGENT_USERNAME)
    d = _disc()
    return build_discussion_prompt(
        _mr_details(),
        DiscussionHistory(discussions=[d], agent=agent),
        d,
        prompt_strategy=strategy,
        base_sha=base_sha,
    )


def _fb(**ov: Any) -> Any:
    return make_settings(prompt_strategy="file-based", **ov)


def _proc(rc: int = 0, stderr: bytes = b"") -> AsyncMock:
    p = AsyncMock()
    p.communicate.return_value = (b"", stderr)
    p.returncode = rc
    return p


# -- Review engine --
class TestBuildReviewPrompt:
    def test_file_based_prompt_structure(self) -> None:
        p = build_review_prompt(
            _review_req(),
            prompt_strategy="file-based",
            base_sha=BASE_SHA,
        )
        assert len(p) < FILE_BASED_MAX
        for ref in (
            ".copilot-review/mr-description.md",
            ".copilot-review/prior-feedback.md",
            ".copilot-review/suppressed-feedback.md",
        ):
            assert ref in p
        assert f"git diff {BASE_SHA} HEAD" in p
        assert f"git log --format='%h %s' {BASE_SHA}..HEAD" in p
        assert f"git diff {BASE_SHA} HEAD -- <path>" in p
        assert MR_TITLE in p
        assert "UNTRUSTED USER CONTENT" in p

    def test_file_based_omits_git_without_base_sha(self) -> None:
        p = build_review_prompt(
            _review_req(),
            prompt_strategy="file-based",
            base_sha=None,
        )
        assert "git diff" not in p
        assert "Git Commands" not in p

    @pytest.mark.parametrize(
        "strategy,present",
        [("file-based", False), ("inline", True)],
    )
    def test_diff_presence(self, strategy: str, present: bool) -> None:
        p = build_review_prompt(
            _review_req(),
            diff_text=DIFF_TEXT,
            prompt_strategy=strategy,
            base_sha=BASE_SHA if strategy == "file-based" else None,
        )
        assert ("--- a/file.py" in p) == present
        assert ("```diff" in p) == present

    @pytest.mark.parametrize(
        "strategy,present",
        [("file-based", False), ("inline", True)],
    )
    def test_description_presence(
        self,
        strategy: str,
        present: bool,
    ) -> None:
        p = build_review_prompt(
            _review_req(description="Very long MR description here"),
            prompt_strategy=strategy,
            base_sha=BASE_SHA if strategy == "file-based" else None,
        )
        assert ("Very long MR description here" in p) == present

    @pytest.mark.parametrize(
        "sha,expect_base,expect_branch",
        [(BASE_SHA, True, False), (None, False, True)],
        ids=["with-base-sha", "without-base-sha"],
    )
    def test_inline_git_fallback(
        self,
        sha: str | None,
        expect_base: bool,
        expect_branch: bool,
    ) -> None:
        p = build_review_prompt(
            _review_req(),
            prompt_strategy="inline",
            base_sha=sha,
        )
        assert (f"git diff {BASE_SHA} HEAD" in p) == expect_base
        branch = f"git diff {TARGET_BRANCH}...{SOURCE_BRANCH}"
        assert (branch in p) == expect_branch

    def test_file_based_incremental_uses_last_reviewed_sha(self) -> None:
        last_sha = "lastrev123"
        p = build_review_prompt(
            _review_req(),
            prompt_strategy="file-based",
            base_sha=BASE_SHA,
            is_incremental=True,
            last_reviewed_sha=last_sha,
        )
        assert f"git diff {last_sha} HEAD" in p
        assert "ONLY the new changes since the last review" in p


# -- Discussion engine --
class TestBuildDiscussionPrompt:
    def test_file_based_prompt_structure(self) -> None:
        p = _discussion_prompt()
        assert len(p) < FILE_BASED_MAX
        for ref in (
            ".copilot-review/mr-description.md",
            ".copilot-review/current-thread.md",
            ".copilot-review/other-discussions.md",
        ):
            assert ref in p
        assert f"git diff {BASE_SHA} HEAD" in p
        assert DISCUSSION_ID in p

    @pytest.mark.parametrize(
        "strategy,thread,diff",
        [("file-based", False, False), ("inline", True, True)],
    )
    def test_content_presence(
        self,
        strategy: str,
        thread: bool,
        diff: bool,
    ) -> None:
        p = _discussion_prompt(strategy=strategy)
        assert (NOTE_BODY in p) == thread
        assert ("import jwt" in p) == diff


# -- Coding engine --
class TestBuildCodingPrompt:
    def test_file_based_prompt_structure(self) -> None:
        p = build_jira_coding_prompt(
            "PROJ-42",
            "Add feature",
            "Long desc...",
            prompt_strategy="file-based",
        )
        assert len(p) < FILE_BASED_MAX
        assert ".copilot-review/jira-issue.md" in p
        assert "PROJ-42" in p

    @pytest.mark.parametrize(
        "strategy,present",
        [("file-based", False), ("inline", True)],
    )
    def test_description_presence(
        self,
        strategy: str,
        present: bool,
    ) -> None:
        p = build_jira_coding_prompt(
            "PROJ-42",
            "Add feature",
            "Sensitive description content",
            prompt_strategy=strategy,
        )
        assert ("Sensitive description content" in p) == present


# -- write_context_file --
class TestWriteContextFile:
    def test_writes_file(self, tmp_path: Path) -> None:
        write_context_file(tmp_path, "mr-description.md", "Some desc")
        text = (_context_dir_for(tmp_path) / "mr-description.md").read_text()
        assert "Some desc" in text

    def test_creates_sibling_directory(self, tmp_path: Path) -> None:
        ctx = _context_dir_for(tmp_path)
        assert not ctx.exists()
        write_context_file(tmp_path, "test.md", "content")
        assert ctx.is_dir()

    def test_skips_empty_content(self, tmp_path: Path) -> None:
        write_context_file(tmp_path, "empty.md", "")
        assert not _context_dir_for(tmp_path).exists()

    def test_skips_whitespace_only(self, tmp_path: Path) -> None:
        write_context_file(tmp_path, "ws.md", "   \n  ")
        assert not _context_dir_for(tmp_path).exists()

    def test_untrusted_header_prepended(self, tmp_path: Path) -> None:
        write_context_file(tmp_path, "test.md", "User content")
        text = (_context_dir_for(tmp_path) / "test.md").read_text()
        assert text.startswith("<!-- UNTRUSTED USER CONTENT")
        assert "User content" in text

    def test_context_dir_is_sibling_not_child(
        self,
        tmp_path: Path,
    ) -> None:
        write_context_file(tmp_path, "test.md", "content")
        ctx = _context_dir_for(tmp_path)
        assert not str(ctx).startswith(str(tmp_path) + "/")
        assert str(ctx) == str(tmp_path) + "-context"


# -- Discussion formatting helpers --
class TestFormatCurrentThread:
    def test_includes_all_notes_and_labels(self) -> None:
        notes = [
            _note(note_id=1, body="First message"),
            _note(
                note_id=2,
                author_id=42,
                body="Reply",
                author_username="dev",
            ),
        ]
        r = _format_current_thread(_disc(notes=notes), AGENT_USER_ID)
        assert "First message" in r
        assert "Reply" in r
        assert DISCUSSION_ID in r
        assert "**Agent**" in r
        assert "**dev**" in r


class TestFormatOtherDiscussions:
    def test_excludes_triggering_discussion(self) -> None:
        d2 = _disc(
            discussion_id="other",
            notes=[_note(body="Other issue")],
        )
        r = _format_other_discussions(
            [_disc(discussion_id="trigger"), d2],
            "trigger",
        )
        assert "Other issue" in r
        assert "trigger" not in r.split("\n", 1)[1]

    def test_returns_empty_when_no_others(self) -> None:
        assert _format_other_discussions([_disc(discussion_id="only")], "only") == ""

    def test_includes_status(self) -> None:
        r = _format_other_discussions(
            [
                _disc(discussion_id="d1", notes=[_note(body="Open")]),
                _disc(
                    discussion_id="d2",
                    notes=[_note(body="Resolved")],
                    is_resolved=True,
                ),
            ],
            "trigger",
        )
        assert "[open]" in r
        assert "[resolved]" in r


# -- Review pipeline --
class TestReviewPipelineFileBased:
    def _pipeline(
        self,
        tmp_path: Path,
        mr: MRDetails | None = None,
        **kw: Any,
    ) -> tuple[ReviewPipeline, ReviewContext]:
        return ReviewPipeline(
            settings=kw.pop("settings", _fb()),
            event=make_task_event(),
            executor=AsyncMock(),
            gl_client=kw.pop(
                "gl_client",
                make_mock_gitlab_client(tmp_path, mr_details=mr or _mr_details()),
            ),
            **kw,
        ), ReviewContext()

    @patch(_REVIEW_EXEC)
    async def test_file_based_prepare(
        self,
        mock_exec: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_exec.return_value = _proc()
        mr = _mr_details(description="Full MR desc verbatim")
        pipeline, ctx = self._pipeline(tmp_path, mr=mr)
        await pipeline.prepare(ctx)

        mock_exec.assert_awaited_once()
        assert mock_exec.call_args[0] == (
            "git",
            "fetch",
            "--depth=1",
            "origin",
            BASE_SHA,
        )
        assert ctx.base_sha == BASE_SHA
        desc = _context_dir_for(tmp_path) / "mr-description.md"
        assert desc.exists()
        assert "Full MR desc verbatim" in desc.read_text()

    @patch(_REVIEW_EXEC)
    async def test_git_fetch_failure_no_crash(
        self,
        mock_exec: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_exec.return_value = _proc(rc=128, stderr=b"fatal")
        pipeline, ctx = self._pipeline(tmp_path)
        await pipeline.prepare(ctx)
        assert ctx.base_sha is None

    async def test_no_files_in_inline_mode(
        self,
        tmp_path: Path,
    ) -> None:
        pipeline, ctx = self._pipeline(
            tmp_path,
            settings=make_settings(prompt_strategy="inline"),
        )
        await pipeline.prepare(ctx)
        assert not _context_dir_for(tmp_path).exists()
        assert ctx.base_sha is None

    @patch(_REVIEW_EXEC)
    async def test_prior_and_suppressed_feedback_files(
        self,
        mock_exec: MagicMock,
        tmp_path: Path,
    ) -> None:
        """prepare() writes feedback files when discussions qualify."""
        mock_exec.return_value = _proc()
        human_id = 42
        # Unresolved inline discussion authored by agent → prior-feedback.md
        prior_disc = _disc(
            discussion_id="disc-prior",
            notes=[
                _note(
                    note_id=600,
                    author_id=AGENT_USER_ID,
                    body="Missing null check here.",
                    position={"new_path": "src/auth.py", "new_line": 10},
                ),
            ],
            is_resolved=False,
            is_inline=True,
        )
        # Resolved inline discussion authored by agent with human resolver → suppressed-feedback.md
        suppressed_disc = _disc(
            discussion_id="disc-suppressed",
            notes=[
                _note(
                    note_id=601,
                    author_id=AGENT_USER_ID,
                    body="Unused import detected.",
                    resolved_by_id=human_id,
                    position={"new_path": "src/utils.py", "new_line": 1},
                ),
            ],
            is_resolved=True,
            is_inline=True,
        )
        discussions = [prior_disc, suppressed_disc]

        creds = AsyncMock()
        creds.resolve_identity.return_value = AgentIdentity(
            user_id=AGENT_USER_ID, username=AGENT_USERNAME
        )
        gl = make_mock_gitlab_client(tmp_path, mr_details=_mr_details(), discussions=discussions)
        pipeline, ctx = self._pipeline(
            tmp_path,
            mr=_mr_details(),
            gl_client=gl,
            credential_registry=creds,
        )
        await pipeline.prepare(ctx)

        ctx_dir = _context_dir_for(tmp_path)
        prior = ctx_dir / "prior-feedback.md"
        assert prior.exists(), "prior-feedback.md should be written"
        prior_text = prior.read_text()
        assert "Missing null check" in prior_text

        suppressed = ctx_dir / "suppressed-feedback.md"
        assert suppressed.exists(), "suppressed-feedback.md should be written"
        suppressed_text = suppressed.read_text()
        assert "Unused import" in suppressed_text


# -- Discussion pipeline --
class TestDiscussionPipelineFileBased:
    def _pipeline(
        self,
        tmp_path: Path,
        mr_desc: str = MR_DESCRIPTION,
    ) -> tuple[DiscussionPipeline, DiscussionContext]:
        d = _disc(notes=[_note(note_id=NOTE_ID, body="Thread content")])
        return DiscussionPipeline(
            settings=_fb(),
            event=make_task_event(
                task_type="discussion",
                note_id=NOTE_ID,
                discussion_id=DISCUSSION_ID,
                note_body="Please explain",
            ),
            executor=AsyncMock(),
            gl_client=make_mock_gitlab_client(
                tmp_path,
                mr_details=_mr_details(description=mr_desc),
                discussions=[d],
            ),
            agent_identity=AgentIdentity(
                user_id=AGENT_USER_ID,
                username=AGENT_USERNAME,
            ),
        ), DiscussionContext()

    @patch(_DISC_EXEC)
    async def test_context_files_and_base_sha(
        self,
        mock_exec: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_exec.return_value = _proc()
        pipeline, ctx = self._pipeline(
            tmp_path,
            mr_desc="MR disc desc",
        )
        await pipeline.prepare(ctx)

        ctx_dir = _context_dir_for(tmp_path)
        assert "MR disc desc" in (ctx_dir / "mr-description.md").read_text()
        thread = (ctx_dir / "current-thread.md").read_text()
        assert "Thread content" in thread
        assert DISCUSSION_ID in thread
        assert ctx.base_sha == BASE_SHA


# -- Coding pipeline --
class TestCodingPipelineFileBased:
    def _pipeline(
        self,
        tmp_path: Path,
        strategy: str = "file-based",
    ) -> tuple[CodingPipeline, CodingContext]:
        return CodingPipeline(
            settings=make_settings(
                prompt_strategy=strategy,
                **JIRA_SETTINGS,
            ),
            issue=JiraIssue(
                id="10042",
                key="PROJ-42",
                fields=JiraIssueFields(
                    summary="Add auth feature",
                    status=JiraStatus(name="AI Ready", id="1"),
                    description="Implement OAuth2 flow with token refresh",
                ),
            ),
            project_mapping=make_resolved_project(),
            executor=AsyncMock(),
            gitlab_client=AsyncMock(),
            jira_client=AsyncMock(),
        ), CodingContext()

    @patch("gitlab_copilot_agent.coding_pipeline.git_unique_branch")
    @patch("gitlab_copilot_agent.coding_pipeline.git_clone")
    async def test_jira_issue_file_written(
        self,
        mock_clone: AsyncMock,
        mock_branch: AsyncMock,
        tmp_path: Path,
    ) -> None:
        mock_clone.return_value = tmp_path
        mock_branch.return_value = "agent/proj-42"
        pipeline, ctx = self._pipeline(tmp_path)
        await pipeline.prepare(ctx)
        c = (_context_dir_for(tmp_path) / "jira-issue.md").read_text()
        assert "PROJ-42" in c
        assert "Add auth feature" in c
        assert "Implement OAuth2 flow with token refresh" in c

    @patch("gitlab_copilot_agent.coding_pipeline.git_unique_branch")
    @patch("gitlab_copilot_agent.coding_pipeline.git_clone")
    async def test_no_file_in_inline_mode(
        self,
        mock_clone: AsyncMock,
        mock_branch: AsyncMock,
        tmp_path: Path,
    ) -> None:
        mock_clone.return_value = tmp_path
        mock_branch.return_value = "agent/proj-42"
        pipeline, ctx = self._pipeline(tmp_path, strategy="inline")
        await pipeline.prepare(ctx)
        assert not _context_dir_for(tmp_path).exists()
