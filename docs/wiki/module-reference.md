# Module Reference

All modules in `src/gitlab_copilot_agent/`, organized by architectural layer.

---

## HTTP Ingestion Layer

### `main.py`
**Purpose**: FastAPI application entrypoint, lifespan management, poller startup.

**Key Functions**:
- `lifespan(app: FastAPI) -> AsyncIterator[None]`: Initialize telemetry, load settings, build `AppContext`, start pollers, graceful shutdown
- `health() -> dict[str, object]`: Health check endpoint, includes GitLab poller status if enabled
- `config_reload(body: RenderedMap, request: Request) -> dict`: Hot-reload project registry from new mapping JSON (requires `X-Gitlab-Token` auth)
- `_cleanup_stale_repos(clone_dir: str | None) -> None`: Remove leftover `mr-review-*` dirs on startup
- `_create_executor(backend: str, settings: Settings | None) -> TaskExecutor`: Factory for LocalTaskExecutor or RemoteTaskExecutor. Supports `dispatch_backend="local"` bypass.

**Key Globals**:
- `app: FastAPI`: FastAPI application instance with lifespan and webhook router

**Internal Imports**: `config/`, `app_context`, `telemetry/`, `gitlab_client`, `gitlab_poller`, `jira_client`, `jira_poller`, `gitlab_webhook`, `concurrency/`, `state`, `task_executor`, `coding_pipeline`, `git/`, `mapping_models`, `credential_registry`, `project_registry`, `dedup`, `events`

**Depended On By**: Deployed as uvicorn entrypoint

---

### `gitlab_webhook.py`
**Purpose**: FastAPI router for GitLab webhooks (merge_request, note). Routes note events to the unified discussion handler via @mention detection.

**Key Functions**:
- `webhook(request: Request, background_tasks: BackgroundTasks, x_gitlab_token: str | None) -> dict[str, str]`: POST endpoint, validates HMAC, dispatches to background handlers
- `_validate_webhook_token(received: str | None, expected: str) -> None`: HMAC comparison using `hmac.compare_digest`
- `_process_review(request: Request, payload: MergeRequestWebhookPayload) -> None`: Background task for MR review. Uses `get_app_context()` for typed access to settings, executor, credential_registry. Resolves per-project `resolution_behavior` from project registry. Calls `run_pipeline(ReviewPipeline(...), ctx)`.
- `_is_agent_directed(payload: NoteWebhookPayload, agent_identity: AgentIdentity, request: Request) -> bool`: Check if note @mentions the agent
- `_process_discussion(request: Request, payload: NoteWebhookPayload, agent_identity: AgentIdentity) -> None`: Background task for discussion interactions. Uses `get_app_context()` for typed access. Resolves per-project `resolution_behavior` from project registry. Calls `run_pipeline(DiscussionPipeline(...), ctx)`.

**Key Constants**:
- `HANDLED_ACTIONS = frozenset({"open", "update", "reopen"})`: MR actions that trigger review

**Internal Imports**: `models`, `pipeline`, `review_pipeline`, `discussion_pipeline`, `discussion_models`, `metrics`, `app_context`, `project_registry`

**Depended On By**: `main.py` (includes router)

---

### `gitlab_poller.py`
**Purpose**: Background poller that discovers open MRs and @mention notes via GitLab API.

**Key Classes**:
- `GitLabPoller`: Polls projects on interval, synthesizes webhook payloads, dispatches to handlers
  - `start() -> None`: Start polling loop
  - `stop() -> None`: Cancel polling task
  - `_poll_once() -> None`: Single poll cycle (all projects, MRs, notes)
  - `_process_mr(project_id: int, mr: MRListItem) -> None`: Dispatch MR review
  - `_process_notes(project_id: int, mrs: list[MRListItem]) -> None`: Dispatch @mention interactions
  - `_watermark: str | None`: ISO timestamp of last poll start (updated after each cycle)
  - `_failures: int`: Consecutive failure count for exponential backoff

**Key Functions**:
- `_build_note_payload(note: NoteListItem, mr: MRListItem, project_id: int, settings: Settings) -> NoteWebhookPayload`: Synthesize webhook payload from API models

**Internal Imports**: `config`, `gitlab_client`, `models`, `pipeline`, `review_pipeline`, `discussion_pipeline`, `concurrency`, `task_executor`, `credential_registry`

**Depended On By**: `main.py` (started in lifespan if `gitlab_poll=true`)

---

### `jira_poller.py`
**Purpose**: Background poller that searches Jira for issues in trigger status.

**Key Protocols**:
- `CodingTaskHandler`: Interface for handling discovered issues
  - `handle(issue: JiraIssue, project_mapping: ResolvedProject) -> None`

**Key Classes**:
- `JiraPoller`: Polls Jira on interval, dispatches to handler
  - `start() -> None`: Start polling loop
  - `stop() -> None`: Cancel polling task
  - `_poll_once() -> None`: Search all mapped projects, invoke handler for new issues
  - `_processed_issues: set[str]`: Issue keys processed in this run

**Internal Imports**: `config`, `jira_client`, `jira_models`, `project_registry`, `telemetry`

**Depended On By**: `main.py` (started in lifespan if Jira configured)

---

## Processing Layer

### `discussion_pipeline.py`
**Purpose**: Discussion interaction pipeline implementation. Handles @mention and thread-reply interactions.

**Key Classes**:
- `DiscussionContext(BasePipelineContext)`: Context for discussion stages
- `DiscussionPipeline`: Implements `Pipeline` — clone, fetch context, LLM, reply ± commit/push ± resolve

**Internal Imports**: `pipeline`, `events`, `git/`, `gitlab_client`, `discussion_engine`, `coding_workflow`, `telemetry/`, `concurrency/`

**Depended On By**: `gitlab_webhook.py`

### `review_pipeline.py`
**Purpose**: MR review pipeline implementation. Orchestrators call `run_pipeline(ReviewPipeline(...), ctx)`.

**Key Classes**:
- `ReviewContext(BasePipelineContext)`: Context for review stages (settings, event, executor, etc.)
- `ReviewPipeline`: Implements `Pipeline` — clone, review via LLM, parse, post comments

**Internal Imports**: `pipeline`, `events`, `git/`, `gitlab_client`, `review_engine`, `comment_parser`, `comment_poster`, `telemetry/`

**Depended On By**: `gitlab_webhook.py`, `gitlab_poller.py`

---

### `discussion_engine.py`
**Purpose**: Discussion prompt construction and response parsing for @mention/thread interactions.

**Key Constants**:
- `MAX_DIFF_CHARS = 80_000`: Max diff characters included in prompt
- `MAX_OTHER_DISCUSSIONS = 5`: Max other threads summarized for context
- `MAX_OTHER_NOTE_CHARS = 100`: Max characters per summarized note

**Key Models**:
- `DiscussionResponse`: Structured LLM response — `reply` (text to post), `has_code_changes` (bool), `resolution` (optional Resolution for thread resolution)

**Key Functions**:
- `build_discussion_prompt(mr_details: MRDetails, discussion_history: DiscussionHistory, triggering_discussion: Discussion) -> str`: Build user prompt with MR metadata, triggering thread, diff, and other discussion context
- `parse_discussion_response(raw: str) -> DiscussionResponse`: Extract structured response from LLM output. Detects `files_changed` JSON blocks for code changes and `resolution` JSON blocks for thread resolution signals
- `run_discussion(executor: TaskExecutor, settings: Settings, repo_path: str, repo_url: str, system_prompt: str, user_prompt: str, source_branch: str) -> TaskResult`: Execute discussion LLM session via executor
- `_parse_resolution(data: dict[str, object]) -> Resolution | None`: Extract Resolution from parsed JSON if present

**Internal Imports**: `task_executor`, `config`, `discussion_models`, `gitlab_client`, `comment_parser`

**Depended On By**: `discussion_pipeline.py`

### `review_engine.py`
**Purpose**: Review prompt construction and execution.

**Key Constants**:
- `REVIEW_SYSTEM_PROMPT: str`: Review system prompt (re-exported from `prompt_defaults.DEFAULT_REVIEW_PROMPT`)
- `MAX_DIFF_CHARS: int`: Maximum characters of diff to include in the prompt before truncation
- `MAX_COMMIT_CHARS: int`: Maximum characters of commit messages to include in the prompt before truncation
- `_SEVERITY_PREFIX_RE: re.Pattern`: Compiled regex to strip severity prefixes (e.g., `**[WARNING]**`) from comments
- `_SUGGESTION_BLOCK_RE: re.Pattern`: Compiled regex to strip suggestion code blocks from comments
- `_PRIOR_FEEDBACK_RULES: str`: Prompt rules instructing the LLM not to duplicate prior feedback
- `_SUPPRESSED_FEEDBACK_RULES: str`: Prompt rules instructing the LLM not to re-raise human-resolved or dismissed items
- `_DISMISSAL_PATTERNS: list[re.Pattern]`: Compiled regexes for dismissal phrase detection (case-insensitive): "won't fix", "intentional", "by design", "not a bug", "false positive", "not an issue", "acceptable risk", "wontfix"
- `_RESOLUTION_EVAL_INSTRUCTIONS`: Prompt instructions for LLM to evaluate whether prior feedback has been addressed

**Key Models**:
- `ReviewRequest`: MR metadata (title, description, source/target branches, commit_messages)

**Key Functions**:
- `build_review_prompt(req: ReviewRequest, diff_text: str | None = None, discussion_history: DiscussionHistory | None = None, is_incremental: bool = False, head_sha: str = "") -> str`: Build user prompt; includes commit messages section when available, diff inline when available, injects prior unresolved feedback with outdated position annotations, labels incremental diffs, and appends suppressed feedback section for human-resolved/dismissed items
- `run_review(executor: TaskExecutor, settings: Settings, repo_path: str, repo_url: str, review_request: ReviewRequest, diff_text: str | None = None, discussion_history: DiscussionHistory | None = None, head_sha: str = "", is_incremental: bool = False) -> TaskResult`: Execute review task and return structured result. Appends `head_sha` to task ID for dedup
- `_format_prior_feedback(history: DiscussionHistory, current_head_sha: str = "") -> str`: Render agent's unresolved inline comments as a prompt section. Includes `[discussion: {id}]` tags for LLM resolution referencing. Annotates comments whose `position.head_sha` differs from `current_head_sha` as outdated
- `_is_human_resolved(disc: Discussion, agent_user_id: int) -> bool`: Returns True when discussion is resolved and the resolver is not the agent (detected via `resolved_by_id` field)
- `_is_dismissed(disc: Discussion, agent_user_id: int) -> bool`: Returns True when a non-agent note matches any `_DISMISSAL_PATTERNS` regex
- `_format_suppressed_feedback(history: DiscussionHistory) -> str`: Render human-resolved (`[MANUALLY RESOLVED]`) and dismissed (`[DISMISSED]`) items as a "Suppressed Feedback (Do Not Re-Raise)" prompt section. Returns empty string when no items qualify
- `_file_line(note: DiscussionNote) -> str`: Format a note's file path and line number for display
- `_strip_comment_formatting(body: str) -> str`: Remove agent-added severity prefix and suggestion blocks from a comment

**Internal Imports**: `config`, `prompt_defaults`, `task_executor`, `discussion_models` (TYPE_CHECKING)

**Depended On By**: `review_engine.py`

---

### `coding_engine.py`
**Purpose**: Coding task prompt construction and .gitignore hygiene.

**Key Constants**:
- `CODING_SYSTEM_PROMPT: str`: Coding system prompt (re-exported from `prompt_defaults.DEFAULT_CODING_PROMPT`)
- `_PYTHON_GITIGNORE_PATTERNS: list[str]`: Standard Python ignore patterns

**Key Functions**:
- `build_jira_coding_prompt(issue_key: str, summary: str, description: str | None) -> str`: Build user prompt from Jira issue
- `ensure_gitignore(repo_root: str) -> bool`: Ensure .gitignore contains Python patterns, returns True if modified
- `run_coding_task(...) -> str`: Ensure .gitignore, execute coding task
- `parse_agent_output(text: str) -> CodingAgentOutput`: Extract structured JSON from agent response (Pydantic-validated `summary` + `files_changed`)

**Internal Imports**: `config`, `prompt_defaults`, `task_executor`

**Depended On By**: `coding_pipeline.py`

---

### `prompt_defaults.py`
**Purpose**: Canonical source of built-in system prompts and configurable prompt resolution.

**Key Types**:
- `PromptType = Literal["coding", "review", "discussion"]`

**Key Constants**:
- `DEFAULT_CODING_PROMPT: str`: Built-in coding system prompt
- `DEFAULT_REVIEW_PROMPT: str`: Built-in review system prompt

**Key Functions**:
- `get_prompt(settings: Settings, prompt_type: PromptType) -> str`: Resolve the effective system prompt for a given type. Resolution: global base (`SYSTEM_PROMPT` + suffix) → type-specific override or built-in default + suffix → combined result.

**Internal Imports**: `config` (TYPE_CHECKING only)

**Depended On By**: `review_engine.py`, `coding_engine.py`, `task_runner.py`

---

### `coding_workflow.py`
**Purpose**: Shared helper for applying coding results (diff passback from k8s pods).

**Key Functions**:
- `apply_coding_result(result: TaskResult, repo_path: Path) -> None`: Validate `base_sha`, apply patch via `git apply --3way` if `CodingResult` has a patch. No-op for local executor (empty patch).

**Internal Imports**: `task_executor`, `git/`, `telemetry/`

**Depended On By**: `coding_pipeline.py`

---

## Execution Layer

### `task_executor.py`
**Purpose**: TaskExecutor protocol and LocalTaskExecutor implementation.

**Key Models**:
- `TaskParams`: Parameters for a Copilot task (task_type, repo_url, branch, prompts, settings, repo_path)
- `TaskResult`: Union type `ReviewResult | CodingResult` (return type for all executors)
- `ReviewResult`: `summary: str` (Pydantic BaseModel, frozen=True)
- `CodingResult`: `summary: str`, `patch: str`, `base_sha: str` (Pydantic BaseModel, frozen=True)

**Key Protocols**:
- `TaskExecutor`: `execute(task: TaskParams) -> TaskResult`

**Key Classes**:
- `LocalTaskExecutor`: Runs `copilot_session.py` in-process, returns `ReviewResult` for reviews, `CodingResult` with empty patch for coding
  - Requires `task.repo_path` to be set

**Internal Imports**: `config`

**Depended On By**: Review, discussion, and coding pipelines; `main.py` (instantiation)

---

### `remote_executor.py`
**Purpose**: Unified remote task executor — claim-check dispatch for any KEDA-backed backend (K8s Jobs or ACA Job executions). Replaces the former `k8s_executor.py` and `aca_executor.py`.

**Key Constants**:
- `_POLL_INTERVAL = 5`: Seconds between result blob checks
- `_LOCK_PREFIX = "remote_exec:"`: Idempotency lock prefix

**Key Functions**:
- `parse_result(raw: str, task_type: str) -> TaskResult`: Parse result JSON or wrap raw string. Handles review, coding, and error result types with traceback logging.

**Key Classes**:
- `RemoteTaskExecutor`: Implements `TaskExecutor`
  - `execute(task: TaskParams) -> TaskResult`: Check cache, check lock, upload tarball, enqueue, poll for result
  - `_poll_result(task: TaskParams) -> TaskResult`: Poll ResultStore until result or timeout

**Internal Imports**: `task_executor`, `git/`, `concurrency/`

**Depended On By**: `main.py` (instantiation when `task_executor=kubernetes`)

---

### `copilot_session.py`
**Purpose**: Copilot SDK wrapper — client init, session config, result extraction.

**Key Constants**:
- `_SDK_ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER"})`: Safe env vars for SDK subprocess

**Key Functions**:
- `build_sdk_env(github_token: str | None) -> dict[str, str]`: Build minimal env dict for SDK subprocess (excludes service secrets)
- `run_copilot_session(settings: Settings, repo_path: str, system_prompt: str, user_prompt: str, timeout: int, task_type: str, validate_response: Callable[[str], str | None] | None) -> str`: Full Copilot session lifecycle
  - Creates CopilotClient with minimal env
  - Discovers repo config (skills, agents, instructions)
  - Injects repo instructions into system prompt
  - Creates session with BYOK provider if configured
  - Sends user prompt, waits for session.idle
  - If `validate_response` returns a string, sends it as a follow-up (one retry max)
  - Returns last assistant message
  - Emits `copilot_session_duration` metric

**Internal Imports**: `config`, `repo_config`, `process_sandbox`, `metrics`, `telemetry`

**Depended On By**: `task_executor.py` (LocalTaskExecutor), `task_runner.py` (K8s Job)

---

### `task_runner.py`
**Purpose**: K8s Job entrypoint (`python -m gitlab_copilot_agent.task_runner`).

**Key Constants**:
- `VALID_TASK_TYPES = frozenset({"review", "coding", "echo"})`
- `_RESULT_TTL = 3600`

**Key Functions**:
- `run_task() -> int`: Main entry point
  - Dequeues task from Azure Storage Queue (or reads env vars for echo tasks)
  - Validates task type
  - Validates `repo_blob_key` starts with `repos/` prefix
  - Downloads repo tarball from blob and extracts to temp dir
  - Calls `run_copilot_session()`
  - For coding tasks: calls `_build_coding_result()` to capture diff
  - Stores result in Azure Blob Storage (JSON-encoded `TaskResult`)
  - Returns exit code 0/1
- `_build_coding_result(response: str, repo_path: Path) -> CodingResult`: Parse `CodingAgentOutput` from response, stage listed files explicitly, capture `git diff --cached --binary`, validate size ≤ `MAX_PATCH_SIZE`, validate patch (no `../`), return `CodingResult`
- `_coding_response_validator(response: str) -> str | None`: Validate agent response contains structured JSON; returns retry prompt if missing
- `_store_result(task_id: str, result: str) -> None`: Persist to Azure Blob Storage with TTL
- `_dequeue_task() -> tuple | None`: Dequeue from Azure Storage Queue if configured
- `_get_required_env(name: str) -> str`: Raise if env var missing
- `_parse_task_payload(raw: str) -> dict[str, str]`: Parse JSON payload

**Security**: Zero GitLab credentials. Repo received via blob transfer from controller.

**Internal Imports**: `config/`, `copilot_session`, `git/`, `coding_engine`, `prompt_defaults`

**Depended On By**: K8s Job container command

---

## External Service Clients

### `gitlab_client.py`
**Purpose**: Async GitLab REST API client using httpx — fully typed, with retry and pagination.

**Key Models**:
- `MRAuthor`: id, username
- `MRListItem`: iid, title, description, source/target branches, sha, web_url, state, author, updated_at
- `NoteListItem`: id, body, author, system, created_at
- `MRDiffRef`: base_sha, start_sha, head_sha
- `MRChange`: old_path, new_path, diff, new_file, deleted_file, renamed_file
- `MRDetails`: title, description, diff_refs, changes
- `MRCommit`: id, title, message (frozen, extra="ignore")

**Key Protocols**:
- `GitLabClientProtocol`: Interface for all GitLab operations

**Key Classes**:
- `GitLabClient`:
  - `__init__(url: str, token: str)`: Initialize httpx async client with PRIVATE-TOKEN header
  - `aclose() -> None`: Close the underlying httpx client
  - `__aenter__`/`__aexit__`: Async context manager support
  - `_request(method, path, *, idempotent, **kwargs) -> Response`: HTTP request with retry on 429/5xx for idempotent (GET) requests; respects `Retry-After` header
  - `_paginate(path, params) -> list[dict]`: Fetch all pages of a paginated endpoint
  - `get_mr_details(project_id, mr_iid) -> MRDetails`: Fetch MR changes; retries on null diff_refs (GitLab race)
  - `clone_repo(clone_url, branch, token, clone_dir) -> Path`: Clone repo via `git/` package
  - `cleanup(repo_path) -> None`: Remove cloned repo
  - `create_merge_request(...) -> int`: Create MR, return iid
  - `post_mr_comment(project_id, mr_iid, body) -> None`: Post MR note
  - `create_mr_discussion(project_id, mr_iid, body, position) -> None`: Create inline discussion on diff
  - `list_project_mrs(project_id, state, updated_after) -> list[MRListItem]`: List MRs (paginated)
  - `list_mr_notes(project_id, mr_iid, created_after) -> list[NoteListItem]`: List notes (paginated)
  - `resolve_project(id_or_path) -> int`: Resolve project ID (URL-encodes paths)
  - `list_mr_discussions(project_id, mr_iid) -> list[Discussion]`: List discussions (paginated)
  - `get_current_user() -> AgentIdentity`: GET /user for authenticated identity
  - `resolve_discussion(project_id, mr_iid, discussion_id) -> None`: PUT to resolve a thread
  - `reply_to_discussion(project_id, mr_iid, discussion_id, body) -> None`: POST reply to thread
  - `compare_commits(project_id, from_sha, to_sha) -> list[MRChange]`: Compare two commits
  - `get_mr_commits(project_id, mr_iid) -> list[MRCommit]`: Fetch MR commits (paginated)

**Internal Imports**: `git/`, `discussion_models`

**Depended On By**: `gitlab_webhook.py`, `gitlab_poller.py`, `main.py`

---

### `jira_client.py`
**Purpose**: Jira REST API v3 client using basic auth.

**Key Protocols**:
- `JiraClientProtocol`: Interface for Jira operations

**Key Classes**:
- `JiraClient`:
  - `__init__(base_url: str, email: str, api_token: str)`: Initialize httpx client with Basic auth
  - `close() -> None`: Close HTTP client
  - `search_issues(jql: str) -> list[JiraIssue]`: Paginated JQL search
  - `transition_issue(issue_key: str, target_status: str) -> None`: Transition by status name
  - `add_comment(issue_key: str, body: str) -> None`: Add plain-text comment (ADF format)

**Internal Imports**: `jira_models`

**Depended On By**: `jira_poller.py`, `coding_pipeline.py`, `main.py`

---

## Shared Utilities

### `git/` package (formerly `git_operations.py`)
**Purpose**: Git CLI wrappers (clone, branch, commit, push, patch, archive, validation). Split from the former `git_operations.py` monolith into focused submodules.

**Submodules**:
- **`clone.py`**: Repository cloning (`git_clone`) with URL validation and credential embedding
- **`operations.py`**: Branch, commit, push operations (`git_create_branch`, `git_unique_branch`, `git_commit`, `git_push`, `git_head_sha`)
- **`patches.py`**: Patch application and staged diff capture (`git_apply_patch`, `git_diff_staged`, `_validate_patch`)
- **`archive.py`**: Repository archiving utilities for remote executor blob transfer
- **`validation.py`**: URL and patch validation (`_validate_clone_url`, `_sanitize_url_for_log`, `MAX_PATCH_SIZE`)

**Key Constants**:
- `CLONE_DIR_PREFIX = "mr-review-"`
- `MAX_PATCH_SIZE = 10 * 1024 * 1024` (10 MB) — maximum allowed patch size for diff passback

**Key Functions** (re-exported from `git/__init__.py`):
- `git_clone(clone_url: str, branch: str, token: str, clone_dir: str | None) -> Path`: Clone repo with embedded credentials, validate URL
- `git_create_branch(repo_path: Path, branch_name: str) -> None`: Create and checkout branch
- `git_unique_branch(repo_path: Path, base_name: str) -> str`: Create branch with collision detection — appends `-2`, `-3`, etc. using `git ls-remote --heads` (works with shallow clones)
- `git_commit(repo_path: Path, message: str, author_name: str, author_email: str) -> bool`: Stage all, commit, return False if nothing to commit
- `git_push(repo_path: Path, remote: str, branch: str, token: str) -> None`: Push with token sanitization
- `git_apply_patch(repo_path: Path, patch: str) -> None`: Apply unified diff with `git apply --3way --binary`
- `git_head_sha(repo_path: Path) -> str`: Get current HEAD commit SHA
- `git_diff_staged(repo_path: Path) -> str`: Capture staged diff (`git diff --cached --binary`), preserves trailing whitespace

**Internal Imports**: `telemetry/`

**Depended On By**: `gitlab_client.py`, `review_pipeline.py`, `discussion_pipeline.py`, `coding_pipeline.py`, `task_runner.py`, `coding_workflow.py`

---

### `comment_parser.py`
**Purpose**: Extract structured review output from Copilot agent response.

**Key Models**:
- `ReviewComment`: file, line, severity, comment, suggestion, suggestion_start_offset, suggestion_end_offset
- `Resolution`: discussion_id, status (resolved/not_addressed/partial), message — resolution determination for prior feedback
- `ParsedReview`: comments, summary, resolutions

**Key Functions**:
- `parse_review(raw: str) -> ParsedReview`: Extract JSON object with `comments` and `resolutions` arrays from code fence or raw text, parse into models, extract summary

**Internal Imports**: None

**Depended On By**: `review_engine.py`, `discussion_engine.py`

---

### `comment_poster.py`
**Purpose**: Post review comments to GitLab MR as inline discussions and summary. Handles resolution actions for prior feedback. Embeds SHA marker in summary note for incremental review tracking. Composes a structured activity summary (posting outcomes + resolution stats) into the summary note.

**Key Functions**:
- `post_review(gitlab_client: gl.Gitlab, project_id: int, mr_iid: int, diff_refs: MRDiffRef, review: ParsedReview, changes: list[MRChange], resolution_behavior: str = "suggest", allowed_discussion_ids: frozenset[str] = frozenset(), head_sha: str = "") -> None`: Post inline comments + resolve/acknowledge prior feedback + summary with SHA marker and activity section
  - Validates comment positions against diff hunks
  - Falls back to note with file:line context if position invalid
  - Tracks posting outcomes (inline, fallback, skipped) via counters
  - Processes resolutions via `_handle_resolutions()` before posting summary
  - When comments or resolutions are nonzero, inserts activity section between summary text and SHA marker
  - When `head_sha` provided, appends `format_sha_marker(head_sha)` to summary note body
- `_build_activity_section(posted_inline: int, posted_fallback: int, resolutions: list[Resolution], resolved_count: int) -> str`: Compose markdown activity summary from posting outcomes and resolution data. Returns empty string when all counts are zero. Includes: new comments (inline + fallback total), threads resolved, partial resolutions — with singular/plural handling
- `_handle_resolutions(mr: object, resolutions: list[Resolution], resolution_behavior: str) -> int`: Process resolutions per configured behavior (auto-resolve/suggest/off). Returns count of resolved threads
- `_parse_hunk_lines(diff: str, new_path: str) -> set[tuple[str, int]]`: Extract valid (file, line) positions from unified diff
- `_is_valid_position(file: str, line: int, valid_positions: set[tuple[str, int]]) -> bool`: Check if position valid

**Internal Imports**: `comment_parser`, `gitlab_client`, `incremental`

**Depended On By**: `review_pipeline.py`

---

### `incremental.py`
**Purpose**: SHA marker utilities for incremental MR review. Embeds and extracts a hidden HTML comment in overview notes to track the last-reviewed commit SHA.

**Key Functions**:
- `extract_last_reviewed_sha(discussion_history: DiscussionHistory | None) -> str | None`: Scans overview notes in reverse chronological order for the SHA marker
- `format_sha_marker(head_sha: str) -> str`: Generates the hidden HTML comment marker

**Key Constants**:
- `_SHA_MARKER_RE`: Regex matching `<!-- mr-review-agent: last_reviewed_sha=([a-f0-9]{7,40}) -->`

**Internal Imports**: `discussion_models` (TYPE_CHECKING only)

**Depended On By**: `review_pipeline.py` (extraction), `comment_poster.py` (formatting)

**ADR**: 0009-incremental-review-sha-marker.md

---

### `repo_config.py`
**Purpose**: Discover repo-level Copilot configuration (skills, agents, instructions).

**Key Constants**:
- `_CONFIG_ROOTS = [".github", ".claude"]`
- `_SKILLS_DIR = "skills"`
- `_AGENTS_DIR = "agents"`
- `_INSTRUCTIONS_DIR = "instructions"`
- `_CONFIG_ROOT_INSTRUCTIONS: dict[str, list[str]]`: Root-specific instruction files
- `_AGENT_SUFFIX = ".agent.md"`
- `_AGENTS_MD = "AGENTS.md"`
- `_CLAUDE_MD = "CLAUDE.md"`

**Key Models**:
- `AgentConfig`: name, prompt, description, tools, display_name, mcp_servers, infer
- `RepoConfig`: skill_directories, custom_agents, instructions

**Key Functions**:
- `discover_repo_config(repo_path: str) -> RepoConfig`: Discover all skills, agents, instructions
  - Scans `.github/` and `.claude/` for skills, agents, instructions
  - Reads `AGENTS.md` (root, then subdirs)
  - Reads `CLAUDE.md` (if not in `.claude/`)
  - Deduplicates symlinks (resolved paths must stay within repo)
- `_parse_agent_file(path: Path) -> AgentConfig | None`: Parse .agent.md with YAML frontmatter
- `_resolve_real_path(path: Path, repo_root: Path) -> Path | None`: Resolve symlinks, reject paths escaping repo

**Internal Imports**: None (external: frontmatter, pydantic)

**Depended On By**: `copilot_session.py`

---

## Data & Configuration

### `models.py`
**Purpose**: Pydantic models for GitLab webhook payloads.

**Key Models**:
- `WebhookUser`: id, username
- `WebhookProject`: id, path_with_namespace, git_http_url
- `MRLastCommit`: id (sha), message
- `MRObjectAttributes`: iid, title, description, action, source/target branches, last_commit, url, oldrev
- `MergeRequestWebhookPayload`: object_kind, user, project, object_attributes
- `NoteObjectAttributes`: note, noteable_type
- `NoteMergeRequest`: iid, title, source/target branches
- `NoteWebhookPayload`: object_kind, user, project, object_attributes, merge_request

All use `strict=True` config.

**Internal Imports**: None

**Depended On By**: `gitlab_webhook.py`, `gitlab_poller.py`, `review_pipeline.py`

---

### `jira_models.py`
**Purpose**: Pydantic models for Jira REST API responses.

**Key Models**:
- `JiraUser`: account_id, display_name, email_address
- `JiraStatus`: name, id
- `JiraIssueFields`: summary, description, status, assignee, labels
- `JiraIssue`: id, key, fields
  - `project_key` property: extract "PROJ" from "PROJ-123"
- `JiraSearchResponse`: issues, next_page_token, total
- `JiraTransition`: id, name
- `JiraTransitionsResponse`: transitions

All use `extra="ignore"` config.

**Internal Imports**: None

**Depended On By**: `jira_client.py`, `jira_poller.py`, `coding_pipeline.py`

---

### `discussion_models.py`
**Purpose**: Pydantic models for MR discussion history, shared by review and discussion flows.

**Key Models**:
- `DiscussionNote`: note_id, author_id, author_username, body, created_at, is_system, resolved, resolvable, position
- `Discussion`: discussion_id, notes, is_resolved, is_inline
- `AgentIdentity`: user_id, username (discovered via `GET /user`)
- `DiscussionHistory`: discussions, agent

All use `frozen=True` config.

**Dependencies**: `pydantic`

**Internal Imports**: None

**Depended On By**: `review_pipeline.py`, `credential_registry.py`, `discussion_pipeline.py`, `discussion_engine.py`

---

### `mapping_models.py`
**Purpose**: Pydantic models for YAML source mappings and rendered JSON format (v1 config).

**Key Models**:
- `MappingSource`: YAML source with `defaults` + `bindings` list
- `RenderedMap`: Flat JSON for `JIRA_PROJECT_MAP` env var and `/config/reload` body
- `RenderedBinding`: Single binding — `repo`, `target_branch`, `credential_ref`

**Depended On By**: `mapping_cli.py`, `project_registry.py`, `main.py`

---

### `config_v2.py`
**Purpose**: GitLab-centric YAML config models (v2). Replaces Jira-keyed `mapping_models.py` for project configuration.

**Key Models**:
- `ConfigFile`: Root model with `version: 2`, `gitlab`, `dispatch`, `copilot`, `server`, `prompts`, `defaults`, `projects`, `integrations`
- `ProjectConfig`: Single GitLab project; all fields except `repo` are optional (fall back to `ConfigDefaults`)
- `JiraIntegrationConfig`: Jira integration referenced by projects via name

**Key Functions**:
- `load_config_file(path: Path | None) -> ConfigFile`: Load + validate YAML, audit-log marketplace URLs (S10)

**Depended On By**: `mapping_cli.py`, `project_registry.py`

---

### `app_context.py`
**Purpose**: Frozen `AppContext` dataclass replacing `app.state` service locator. Provides typed dependency injection.

**Key Types**:
- `AppContext`: Frozen dataclass holding `settings`, `executor`, `repo_locks`, `dedup_store`, `dedup`, `credential_registry`, `allowed_project_ids`

**Key Functions**:
- `get_app_context(request: Request) -> AppContext`: FastAPI `Depends()` accessor

**Depended On By**: `gitlab_webhook.py`, `main.py`

---

### `credential_registry.py`
**Purpose**: Resolve credential aliases to GitLab tokens from environment. TTL-cached identity resolution via httpx.

**Key Methods**:
- `from_env() -> CredentialRegistry`: Reads `GITLAB_TOKEN` + `GITLAB_TOKEN__<ALIAS>` env vars
- `resolve(credential_ref: str) -> str`: Returns token for alias, raises `KeyError` if unknown
- `resolve_identity(credential_ref: str, gitlab_url: str) -> AgentIdentity`: TTL-cached identity lookup (default 1hr, `time.monotonic()`). Uses httpx `GET /api/v4/user`.

**Depended On By**: `project_registry.py`, `main.py`, `gitlab_poller.py`

---

### `project_registry.py`
**Purpose**: Fully resolved project context for runtime use.

**Key Types**:
- `ResolvedProject`: Frozen Pydantic model — `jira_project` (optional), `repo`, `gitlab_project_id`, `clone_url`, `target_branch`, `credential_ref`, `token` (masked in repr)
- `ProjectRegistry`: Registry with `from_rendered_map()` (v1) and `from_config()` (v2) async factories, `get_by_jira()`, `get_by_project_id()`, `jira_keys()`

**Depended On By**: `jira_poller.py`, `coding_pipeline.py`, `main.py`

---

### `config/` package (formerly `config.py`)
**Purpose**: Application configuration via environment variables. Split from `config.py` into a package for better organization.

**Submodules**:
- **`settings.py`**: `Settings` (BaseSettings) — all env vars (see configuration-reference.md), `JiraSettings`
- **`runner_settings.py`**: `TaskRunnerSettings` for K8s Job entrypoint configuration
- **`base.py`**: Shared mixins (`CopilotSettingsMixin`, `PromptSettingsMixin`, `DispatchSettingsMixin`) used by both `Settings` and `TaskRunnerSettings`
- **`validators.py`**: Cross-field validators (auth checks, state backend validation, project list validation)

**Key Models** (re-exported from `config/__init__.py`):
- `JiraSettings`: url, email, api_token, trigger_status, in_progress_status, poll_interval, project_map_json
- `Settings` (BaseSettings): All env vars (see configuration-reference.md)
  - `jira` property: return JiraSettings if all required fields set, else None
  - `_check_auth()` validator: ensure either GITHUB_TOKEN or COPILOT_PROVIDER_TYPE set; validate REDIS_URL if backend=redis; validate GITLAB_PROJECTS if gitlab_poll=true

**Internal Imports**: None

**Depended On By**: All modules

---

## State & Concurrency

### `concurrency/` package (formerly `concurrency.py`)
**Purpose**: In-memory locking and deduplication primitives. Split from `concurrency.py` into a package.

**Submodules**:
- **`protocols.py`**: `DistributedLock` and `DeduplicationStore` protocol definitions, `TaskQueue`, `QueueMessage`
- **`memory.py`**: `MemoryLock`, `MemoryDedup` in-memory implementations

**Key Protocols**:
- `DistributedLock`: `acquire(key: str, ttl_seconds: int) -> AbstractAsyncContextManager[None]`, `aclose()`
- `DeduplicationStore`: `is_seen(key: str) -> bool`, `mark_seen(key: str, ttl_seconds: int) -> None`, `aclose()`

**Key Classes**:
- `MemoryLock`: Async lock per key with LRU eviction
  - `acquire(key: str, ttl_seconds: int)`: Context manager, evicts unlocked entries after release
- `MemoryDedup`: In-memory seen set with size-based eviction
  - `is_seen(key: str) -> bool`, `mark_seen(key: str, ttl_seconds: int) -> None`

**Aliases**:
- `RepoLockManager = MemoryLock` (backward compatibility)

**Internal Imports**: None

**Depended On By**: `main.py`, `gitlab_webhook.py`, `gitlab_poller.py`, `coding_pipeline.py`, `dedup.py`

---

### `dedup.py`
**Purpose**: Unified deduplication service consolidating all dedup logic into a single interface. Replaces the former `ReviewedMRTracker` and `ProcessedIssueTracker` classes.

**Key Classes**:
- `DeduplicationService`: Wraps a `DeduplicationStore` with typed helpers for each event kind
  - `is_review_seen(project_id: int, mr_iid: int, head_sha: str) -> bool`
  - `mark_review(project_id: int, mr_iid: int, head_sha: str) -> None`
  - `is_note_seen(project_id: int, mr_iid: int, note_id: int) -> bool`
  - `mark_note(project_id: int, mr_iid: int, note_id: int) -> None`
  - `is_issue_seen(issue_key: str) -> bool`
  - `mark_issue(issue_key: str) -> None`

**Internal Imports**: `concurrency/`

**Depended On By**: `gitlab_webhook.py`, `gitlab_poller.py`, `jira_poller.py`, `app_context.py`

---

### `events.py`
**Purpose**: Unified internal event model. Provides `TaskEvent` Pydantic model that replaces direct webhook payload passing between ingestion and processing layers.

**Key Models**:
- `TaskEvent`: Pydantic model representing a normalized event from any ingestion source (webhook, poller). Orchestrators and pipelines receive `TaskEvent` instead of raw webhook payloads.

**Internal Imports**: `models`

**Depended On By**: `gitlab_webhook.py`, `gitlab_poller.py`, `review_pipeline.py`, `discussion_pipeline.py`, `coding_pipeline.py`

---

### `pipeline.py`
**Purpose**: Pipeline protocol and runner for structured multi-stage processing. Defines a 4-stage protocol (prepare, execute, process, cleanup) and a `run_pipeline()` function that drives any conforming pipeline.

**Key Protocols**:
- `Pipeline`: Protocol with stages `prepare()`, `execute()`, `process()`, `cleanup()`

**Key Classes**:
- `BasePipelineContext`: Base dataclass for pipeline stage context

**Key Functions**:
- `run_pipeline(pipeline: Pipeline, context: BasePipelineContext) -> None`: Sequential runner that calls each stage, with cleanup in finally block

**Internal Imports**: None

**Depended On By**: `review_pipeline.py`, `discussion_pipeline.py`, `coding_pipeline.py`

---

### `review_pipeline.py`
**Purpose**: MR review pipeline implementation. Orchestrators call `run_pipeline(ReviewPipeline(...), ctx)`.

**Key Classes**:
- `ReviewContext(BasePipelineContext)`: Context for review stages (settings, event, executor, etc.)
- `ReviewPipeline`: Implements `Pipeline` — clone, review via LLM, parse, post comments

**Internal Imports**: `pipeline`, `events`, `git/`, `gitlab_client`, `review_engine`, `comment_parser`, `comment_poster`, `telemetry/`

**Depended On By**: `gitlab_webhook.py`, `gitlab_poller.py`

---

### `discussion_pipeline.py`
**Purpose**: Discussion interaction pipeline implementation. Handles @mention and thread-reply interactions.

**Key Classes**:
- `DiscussionContext(BasePipelineContext)`: Context for discussion stages
- `DiscussionPipeline`: Implements `Pipeline` — clone, fetch context, LLM, reply ± commit/push ± resolve

**Internal Imports**: `pipeline`, `events`, `git/`, `gitlab_client`, `discussion_engine`, `coding_workflow`, `telemetry/`, `concurrency/`

**Depended On By**: `gitlab_webhook.py`, `gitlab_poller.py`

---

### `coding_pipeline.py`
**Purpose**: Jira coding task pipeline implementation. Handles issue-to-MR workflow.

**Key Classes**:
- `CodingContext(BasePipelineContext)`: Context for coding stages
- `CodingPipeline`: Implements `Pipeline` — clone, branch, code via LLM, apply result, commit, push, create MR

**Internal Imports**: `pipeline`, `events`, `git/`, `gitlab_client`, `jira_client`, `coding_engine`, `coding_workflow`, `telemetry/`, `concurrency/`

**Depended On By**: `jira_poller.py` (as Jira poller handler)

---

### `state.py`
**Purpose**: Factory functions for concurrency primitives (lock, dedup, result store, task queue). Uses in-memory implementations for lock/dedup and delegates to Azure Storage for result store and task queue when configured.

**Key Functions**:
- `create_lock() -> DistributedLock`: Factory — returns `MemoryLock` (single-controller deployment)
- `create_dedup() -> DeduplicationStore`: Factory — returns `MemoryDedup` (single-controller deployment)
- `create_result_store(*, azure_storage_account_url, azure_storage_connection_string, task_blob_container) -> ResultStore`: Factory — returns `BlobResultStore` when Azure Storage is configured, otherwise `MemoryResultStore`
- `create_task_queue(*, azure_storage_queue_url, azure_storage_account_url, azure_storage_connection_string, task_queue_name, task_blob_container) -> TaskQueue`: Factory — returns `AzureStorageTaskQueue` when Azure Storage is configured, otherwise `MemoryTaskQueue`

**Internal Imports**: `concurrency/`, `azure_storage` (lazy)

**Depended On By**: `main.py`

---

## Telemetry

### `telemetry/` package (formerly `telemetry.py`)
**Purpose**: OpenTelemetry tracing, metrics, and log export setup. Split from `telemetry.py` into a package.

**Submodules**:
- **`tracing.py`**: TracerProvider setup, `get_tracer()`, span utilities
- **`logging.py`**: Structlog configuration, `add_trace_context()` processor, `emit_to_otel_logs()` processor
- **`exporters.py`**: OTLP exporter configuration (gRPC and HTTP/protobuf)
- **`_state.py`**: Module-level state for provider instances

**Key Constants**:
- `_SERVICE_NAME = "gitlab-copilot-agent"`

**Key Functions** (re-exported from `telemetry/__init__.py`):
- `init_telemetry() -> None`: Configure OTEL providers, exporters, auto-instrumentation (FastAPI, httpx)
  - No-op if OTEL_EXPORTER_OTLP_ENDPOINT unset
  - Sets up TracerProvider, MeterProvider, LoggerProvider
  - Exports via OTLP gRPC
- `shutdown_telemetry() -> None`: Flush and shutdown providers
- `get_tracer(name: str) -> trace.Tracer`: Get tracer instance
- `add_trace_context(logger, method, event_dict) -> dict`: Structlog processor injecting trace_id, span_id
- `emit_to_otel_logs(logger, method, event_dict) -> dict`: Structlog processor re-emitting to stdlib logging for OTLP export

**Internal Imports**: None

**Depended On By**: `main.py`, `copilot_session.py`, `git/`, `jira_poller.py`, `review_pipeline.py`, `discussion_pipeline.py`, `coding_pipeline.py`

---

### `metrics.py`
**Purpose**: Shared OTel metrics instruments.

**Key Constants**:
- `METER_NAME = "gitlab_copilot_agent"`

**Key Metrics**:
- `reviews_total` (Counter): Total MR reviews processed (labels: outcome)
- `reviews_duration` (Histogram): MR review duration in seconds (labels: outcome)
- `coding_tasks_total` (Counter): Total coding tasks processed (labels: outcome)
- `coding_tasks_duration` (Histogram): Coding task duration in seconds (labels: outcome)
- `webhook_received_total` (Counter): Total webhooks received (labels: object_kind)
- `webhook_errors_total` (Counter): Webhook background errors (labels: handler)
- `copilot_session_duration` (Histogram): Copilot session duration in seconds (labels: task_type)

**Internal Imports**: None

**Depended On By**: `copilot_session.py`, `gitlab_webhook.py`

---

### `process_sandbox.py`
**Purpose**: Copilot CLI binary resolution.

**Key Functions**:
- `_get_real_cli_path() -> str`: Resolve bundled Copilot CLI binary path from github-copilot-sdk package

**Internal Imports**: None

**Depended On By**: `copilot_session.py`

---

### `plugin_manager.py`
**Purpose**: Runtime plugin installation into isolated per-session HOME directories.

**Key Functions**:
- `setup_plugins(home_dir, plugins, marketplaces)`: Install marketplaces and plugins into an isolated HOME
- `add_marketplace(home_dir, marketplace_url)`: Register a custom plugin marketplace
- `install_plugin(home_dir, plugin_spec)`: Install a single Copilot CLI plugin
- `_run_cli(args, home_dir, timeout)`: Execute a copilot CLI command with timeout and kill-on-timeout
- `_sanitize_url(url)`: Strip credentials and query params from URLs for safe logging

**Internal Imports**: `process_sandbox.get_real_cli_path`
**Depended On By**: `copilot_session.py`

---

## Summary Table

| Module | Layer | LOC (approx) | Key Responsibility |
|--------|-------|--------------|---------------------|
| `main.py` | Ingestion | 168 | FastAPI app, lifespan, pollers |
| `gitlab_webhook.py` | Ingestion | 117 | Webhook endpoint, HMAC validation |
| `gitlab_poller.py` | Ingestion | 175 | MR/note discovery |
| `jira_poller.py` | Ingestion | 90 | Issue discovery |
| `review_engine.py` | Processing | 160 | Review prompt construction |
| `coding_engine.py` | Processing | 109 | Coding prompt construction |
| `prompt_defaults.py` | Processing | 164 | System prompt defaults & resolution |
| `discussion_engine.py` | Processing | 147 | Discussion prompt construction & response parsing |
| `coding_workflow.py` | Processing | ~80 | Shared helper for applying coding results |
| `pipeline.py` | Processing | ~120 | Pipeline protocol + runner |
| `review_pipeline.py` | Processing | ~180 | Review pipeline implementation |
| `discussion_pipeline.py` | Processing | ~220 | Discussion pipeline implementation |
| `coding_pipeline.py` | Processing | ~150 | Coding pipeline implementation |
| `task_executor.py` | Execution | 53 | TaskExecutor protocol |
| `remote_executor.py` | Execution | 162 | Unified claim-check dispatch (K8s + ACA) |
| `copilot_session.py` | Execution | 143 | SDK wrapper |
| `task_runner.py` | Execution | 134 | K8s Job entrypoint |
| `gitlab_client.py` | Clients | 224 | GitLab API client |
| `jira_client.py` | Clients | 117 | Jira API client |
| `git/` | Utils | ~250 | Git CLI wrappers (clone, branch, commit, push, patch, archive) |
| `comment_parser.py` | Utils | 75 | Review parsing |
| `comment_poster.py` | Utils | 184 | Comment posting + activity summary |
| `repo_config.py` | Utils | 184 | Repo config discovery |
| `models.py` | Data | 79 | Webhook models |
| `jira_models.py` | Data | 87 | Jira API models |
| `discussion_models.py` | Data | 71 | MR discussion history models |
| `config/` | Data | ~200 | Settings (env vars, mixins, validators) |
| `concurrency/` | State | ~150 | In-memory locks/dedup protocols + implementations |
| `dedup.py` | State | ~80 | Unified DeduplicationService |
| `events.py` | Data | ~80 | TaskEvent internal event model |
| `state.py` | State | 79 | Factory functions for concurrency primitives |
| `telemetry/` | Telemetry | ~180 | OTEL setup (tracing, logging, exporters) |
| `metrics.py` | Telemetry | 52 | Metrics instruments |
| `process_sandbox.py` | Utils | 20 | CLI path resolution |
| `plugin_manager.py` | Utils | 88 | Plugin installation |

**Total: 35 modules/packages, ~4,500 lines of code**
