# Module Reference

All modules in `src/gitlab_copilot_agent/`, organized by architectural layer.

---

## HTTP Ingestion Layer

### `main.py`
**Purpose**: FastAPI application entrypoint, lifespan management, poller startup.

**Key Functions**:
- `lifespan(app: FastAPI) -> AsyncIterator[None]`: Initialize telemetry, load settings, start pollers, cleanup on shutdown
- `health() -> dict[str, object]`: Health check endpoint, includes GitLab poller status if enabled
- `_cleanup_stale_repos(clone_dir: str | None) -> None`: Remove leftover `mr-review-*` dirs on startup
- `_create_executor(backend: str, settings: Settings | None) -> TaskExecutor`: Factory for LocalTaskExecutor or KubernetesTaskExecutor

**Key Globals**:
- `app: FastAPI`: FastAPI application instance with lifespan and webhook router

**Internal Imports**: `config`, `telemetry`, `gitlab_client`, `gitlab_poller`, `jira_client`, `jira_poller`, `webhook`, `concurrency`, `redis_state`, `task_executor`, `coding_orchestrator`, `git_operations`, `project_mapping`

**Depended On By**: Deployed as uvicorn entrypoint

---

### `webhook.py`
**Purpose**: FastAPI router for GitLab webhooks (merge_request, note).

**Key Functions**:
- `webhook(request: Request, background_tasks: BackgroundTasks, x_gitlab_token: str | None) -> dict[str, str]`: POST endpoint, validates HMAC, dispatches to background handlers
- `_validate_webhook_token(received: str | None, expected: str) -> None`: HMAC comparison using `hmac.compare_digest`
- `_process_review(request: Request, payload: MergeRequestWebhookPayload) -> None`: Background task for MR review
- `_process_copilot_comment(request: Request, payload: NoteWebhookPayload) -> None`: Background task for /copilot commands

**Key Constants**:
- `HANDLED_ACTIONS = frozenset({"open", "update"})`: MR actions that trigger review

**Internal Imports**: `models`, `orchestrator`, `mr_comment_handler`, `metrics`, `concurrency`

**Depended On By**: `main.py` (includes router)

---

### `gitlab_poller.py`
**Purpose**: Background poller that discovers open MRs and /copilot notes via GitLab API.

**Key Classes**:
- `GitLabPoller`: Polls projects on interval, synthesizes webhook payloads, dispatches to handlers
  - `start() -> None`: Start polling loop
  - `stop() -> None`: Cancel polling task
  - `_poll_once() -> None`: Single poll cycle (all projects, MRs, notes)
  - `_process_mr(project_id: int, mr: MRListItem) -> None`: Dispatch MR review
  - `_process_notes(project_id: int, mrs: list[MRListItem]) -> None`: Dispatch /copilot commands
  - `_watermark: str | None`: ISO timestamp of last poll start (updated after each cycle)
  - `_failures: int`: Consecutive failure count for exponential backoff

**Key Functions**:
- `_build_note_payload(note: NoteListItem, mr: MRListItem, project_id: int, settings: Settings) -> NoteWebhookPayload`: Synthesize webhook payload from API models

**Internal Imports**: `config`, `gitlab_client`, `models`, `orchestrator`, `mr_comment_handler`, `concurrency`, `task_executor`

**Depended On By**: `main.py` (started in lifespan if `gitlab_poll=true`)

---

### `jira_poller.py`
**Purpose**: Background poller that searches Jira for issues in trigger status.

**Key Protocols**:
- `CodingTaskHandler`: Interface for handling discovered issues
  - `handle(issue: JiraIssue, project_mapping: GitLabProjectMapping) -> None`

**Key Classes**:
- `JiraPoller`: Polls Jira on interval, dispatches to handler
  - `start() -> None`: Start polling loop
  - `stop() -> None`: Cancel polling task
  - `_poll_once() -> None`: Search all mapped projects, invoke handler for new issues
  - `_processed_issues: set[str]`: Issue keys processed in this run

**Internal Imports**: `config`, `jira_client`, `jira_models`, `project_mapping`, `telemetry`

**Depended On By**: `main.py` (started in lifespan if Jira configured)

---

## Processing Layer

### `orchestrator.py`
**Purpose**: MR review orchestration — clone, review, parse, post comments.

**Key Functions**:
- `handle_review(settings: Settings, payload: MergeRequestWebhookPayload, executor: TaskExecutor) -> None`: Full review pipeline with OTEL span and metrics
  - Clones repo
  - Calls `run_review()` via executor
  - Parses structured review
  - Fetches MR details and posts comments
  - Emits `reviews_total` and `reviews_duration` metrics

**Internal Imports**: `config`, `models`, `task_executor`, `gitlab_client`, `review_engine`, `comment_parser`, `comment_poster`, `metrics`, `telemetry`

**Depended On By**: `webhook.py`, `gitlab_poller.py`

---

### `mr_comment_handler.py`
**Purpose**: Handle `/copilot <instruction>` commands from MR comments.

**Key Constants**:
- `COPILOT_PREFIX = "/copilot "`
- `AGENT_AUTHOR_NAME = "Copilot Agent"`
- `AGENT_AUTHOR_EMAIL = "copilot-agent@noreply.gitlab.com"`

**Key Functions**:
- `parse_copilot_command(note: str) -> str | None`: Extract instruction from comment
- `build_mr_coding_prompt(instruction: str, mr_title: str, source_branch: str, target_branch: str) -> str`: Build user prompt
- `handle_copilot_comment(settings: Settings, payload: NoteWebhookPayload, executor: TaskExecutor, repo_locks: DistributedLock | None) -> None`: Clone → code → apply result → commit → push → comment

**Internal Imports**: `config`, `models`, `task_executor`, `gitlab_client`, `git_operations`, `coding_engine`, `coding_workflow`, `concurrency`, `telemetry`

**Depended On By**: `webhook.py`, `gitlab_poller.py`

---

### `coding_orchestrator.py`
**Purpose**: Jira issue implementation — clone, code, branch, MR, update Jira.

**Key Constants**:
- `AGENT_AUTHOR_NAME = "Copilot Agent"`
- `AGENT_AUTHOR_EMAIL = "copilot-agent@noreply.gitlab.com"`

**Key Classes**:
- `CodingOrchestrator`: Implements `CodingTaskHandler` protocol
  - `handle(issue: JiraIssue, project_mapping: GitLabProjectMapping) -> None`: Full coding pipeline
    - Transitions issue to "In Progress"
    - Clones repo, creates branch `agent/{issue-key}`
    - Calls `run_coding_task()` via executor
    - Calls `apply_coding_result()` to apply diff (K8s executor only)
    - Commits, pushes, creates MR
    - Adds Jira comment with MR URL
    - Emits `coding_tasks_total` and `coding_tasks_duration` metrics

**Internal Imports**: `config`, `gitlab_client`, `jira_client`, `jira_models`, `project_mapping`, `task_executor`, `git_operations`, `coding_engine`, `coding_workflow`, `metrics`, `telemetry`, `concurrency`

**Depended On By**: `main.py` (as Jira poller handler)

---

### `review_engine.py`
**Purpose**: Review prompt construction and execution.

**Key Constants**:
- `SYSTEM_PROMPT: str`: System prompt for code review agent (instructs JSON output format with suggestions)

**Key Models**:
- `ReviewRequest`: MR metadata (title, description, source/target branches)

**Key Functions**:
- `build_review_prompt(req: ReviewRequest) -> str`: Build user prompt instructing agent to run `git diff`
- `run_review(executor: TaskExecutor, settings: Settings, repo_path: str, repo_url: str, review_request: ReviewRequest) -> str`: Execute review task and return raw response

**Internal Imports**: `config`, `task_executor`

**Depended On By**: `orchestrator.py`

---

### `coding_engine.py`
**Purpose**: Coding task prompt construction and .gitignore hygiene.

**Key Constants**:
- `CODING_SYSTEM_PROMPT: str`: System prompt for coding agent (workflow, guidelines, output format)
- `_PYTHON_GITIGNORE_PATTERNS: list[str]`: Standard Python ignore patterns

**Key Functions**:
- `build_jira_coding_prompt(issue_key: str, summary: str, description: str | None) -> str`: Build user prompt from Jira issue
- `ensure_gitignore(repo_root: str) -> bool`: Ensure .gitignore contains Python patterns, returns True if modified
- `run_coding_task(...) -> str`: Ensure .gitignore, execute coding task

**Internal Imports**: `config`, `task_executor`

**Depended On By**: `coding_orchestrator.py`, `mr_comment_handler.py`

---

### `coding_workflow.py`
**Purpose**: Shared helper for applying coding results (diff passback from k8s pods).

**Key Functions**:
- `apply_coding_result(result: TaskResult, repo_path: Path) -> None`: Validate `base_sha`, apply patch via `git apply --3way` if `CodingResult` has a patch. No-op for local executor (empty patch).

**Internal Imports**: `task_executor`, `git_operations`, `telemetry`

**Depended On By**: `coding_orchestrator.py`, `mr_comment_handler.py`

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

**Depended On By**: All execution callers (`orchestrator.py`, `mr_comment_handler.py`, `coding_orchestrator.py`); `main.py` (instantiation)

---

### `k8s_executor.py`
**Purpose**: KubernetesTaskExecutor — dispatches tasks as K8s Jobs, reads results from Redis.

**Key Constants**:
- `_RESULT_KEY_PREFIX = "result:"`
- `_JOB_POLL_INTERVAL = 2`
- `_TTL_AFTER_FINISHED = 300`
- `_ANNOTATION_KEY = "results.copilot-agent/summary"`

**Key Classes**:
- `KubernetesTaskExecutor`: Implements `TaskExecutor`
  - `execute(task: TaskParams) -> TaskResult`: Create Job, poll for completion, retrieve result from Redis (parses JSON as `TaskResult`)
  - `_create_job(job_name: str, task: TaskParams) -> None`: Create K8s Job with task env vars and optional hostAliases
  - `_read_job_status(job_name: str) -> str`: Return "succeeded", "failed", or "running"
  - `_read_job_annotation(job_name: str) -> str | None`: Read result annotation
  - `_read_pod_logs(job_name: str) -> str`: Read pod logs for failed Job
  - `_delete_job(job_name: str) -> None`: Delete Job after timeout/failure
  - `_wait_for_result(redis_client: Redis, job_name: str, task: TaskParams) -> TaskResult`: Poll Redis and Job status

**Key Functions**:
- `_sanitize_job_name(task_type: str, task_id: str) -> str`: Build k8s-compliant Job name
- `_build_env(task: TaskParams, settings: Settings) -> list[dict[str, str]]`: Env vars for Job container
- `_parse_host_aliases(raw: str) -> list[object] | None`: Parse K8S_JOB_HOST_ALIASES JSON

**Internal Imports**: `config`, `task_executor`

**Depended On By**: `main.py` (instantiation when `task_executor=kubernetes`)

---

### `copilot_session.py`
**Purpose**: Copilot SDK wrapper — client init, session config, result extraction.

**Key Constants**:
- `_SDK_ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER"})`: Safe env vars for SDK subprocess

**Key Functions**:
- `build_sdk_env(github_token: str | None) -> dict[str, str]`: Build minimal env dict for SDK subprocess (excludes service secrets)
- `run_copilot_session(settings: Settings, repo_path: str, system_prompt: str, user_prompt: str, timeout: int, task_type: str) -> str`: Full Copilot session lifecycle
  - Creates CopilotClient with minimal env
  - Discovers repo config (skills, agents, instructions)
  - Injects repo instructions into system prompt
  - Creates session with BYOK provider if configured
  - Sends user prompt, waits for session.idle
  - Returns last assistant message
  - Emits `copilot_session_duration` metric

**Internal Imports**: `config`, `repo_config`, `process_sandbox`, `metrics`, `telemetry`

**Depended On By**: `task_executor.py` (LocalTaskExecutor), `task_runner.py` (K8s Job)

---

### `task_runner.py`
**Purpose**: K8s Job entrypoint (`python -m gitlab_copilot_agent.task_runner`).

**Key Constants**:
- `VALID_TASK_TYPES = frozenset({"review", "coding", "echo"})`
- `_RESULT_KEY_PREFIX = "result:"`
- `_RESULT_TTL = 3600`

**Key Functions**:
- `run_task() -> int`: Main entry point
  - Reads env vars: TASK_TYPE, TASK_ID, REPO_URL, BRANCH, TASK_PAYLOAD, REDIS_URL
  - Validates task type
  - Validates REPO_URL matches GITLAB_URL (host + port)
  - Clones repo
  - Calls `run_copilot_session()`
  - For coding tasks: calls `_build_coding_result()` to capture diff
  - Stores result in Redis (JSON-encoded `TaskResult`)
  - Returns exit code 0/1
- `_build_coding_result(summary: str, repo_path: Path) -> CodingResult`: Capture `git diff --cached --binary`, validate size ≤ `MAX_PATCH_SIZE`, validate patch (no `../`), return `CodingResult`
- `_store_result(task_id: str, result: str) -> None`: Persist to Redis with TTL
- `_get_required_env(name: str) -> str`: Raise if env var missing
- `_parse_task_payload(raw: str) -> dict[str, str]`: Parse JSON payload
- `_validate_repo_url(repo_url: str, gitlab_url: str) -> None`: Ensure repo_url authority matches gitlab_url

**Internal Imports**: `config`, `copilot_session`, `git_operations`, `coding_engine`, `review_engine`, `task_executor`

**Depended On By**: K8s Job container command

---

## External Service Clients

### `gitlab_client.py`
**Purpose**: GitLab API client for repo cloning, diff fetching, and MR metadata.

**Key Models**:
- `MRAuthor`: id, username
- `MRListItem`: iid, title, description, source/target branches, sha, web_url, state, author, updated_at
- `NoteListItem`: id, body, author, system, created_at
- `MRDiffRef`: base_sha, start_sha, head_sha
- `MRChange`: old_path, new_path, diff, new_file, deleted_file, renamed_file
- `MRDetails`: title, description, diff_refs, changes

**Key Protocols**:
- `GitLabClientProtocol`: Interface for all GitLab operations

**Key Classes**:
- `GitLabClient`:
  - `__init__(url: str, token: str)`: Initialize python-gitlab client
  - `get_mr_details(project_id: int, mr_iid: int) -> MRDetails`: Fetch MR changes and diff refs
  - `clone_repo(clone_url: str, branch: str, token: str, clone_dir: str | None) -> Path`: Clone repo via git_operations
  - `cleanup(repo_path: Path) -> None`: Remove cloned repo
  - `create_merge_request(...) -> int`: Create MR, return iid
  - `post_mr_comment(project_id: int, mr_iid: int, body: str) -> None`: Post MR note
  - `list_project_mrs(project_id: int, state: str, updated_after: str | None) -> list[MRListItem]`: List MRs
  - `list_mr_notes(project_id: int, mr_iid: int, created_after: str | None) -> list[NoteListItem]`: List notes
  - `resolve_project(id_or_path: str | int) -> int`: Resolve project ID

**Internal Imports**: `git_operations`

**Depended On By**: `orchestrator.py`, `mr_comment_handler.py`, `coding_orchestrator.py`, `gitlab_poller.py`, `main.py`

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

**Depended On By**: `jira_poller.py`, `coding_orchestrator.py`, `main.py`

---

## Shared Utilities

### `git_operations.py`
**Purpose**: Git CLI wrappers (clone, branch, commit, push).

**Key Constants**:
- `CLONE_DIR_PREFIX = "mr-review-"`
- `_GIT_TIMEOUT = 60`
- `MAX_PATCH_SIZE = 10 * 1024 * 1024` (10 MB) — maximum allowed patch size for diff passback

**Key Functions**:
- `git_clone(clone_url: str, branch: str, token: str, clone_dir: str | None) -> Path`: Clone repo with embedded credentials, validate URL
- `git_create_branch(repo_path: Path, branch_name: str) -> None`: Create and checkout branch
- `git_commit(repo_path: Path, message: str, author_name: str, author_email: str) -> bool`: Stage all, commit, return False if nothing to commit
- `git_push(repo_path: Path, remote: str, branch: str, token: str) -> None`: Push with token sanitization
- `git_apply_patch(repo_path: Path, patch: str) -> None`: Apply unified diff with `git apply --3way --binary`
- `git_head_sha(repo_path: Path) -> str`: Get current HEAD commit SHA
- `git_diff_staged(repo_path: Path) -> str`: Capture staged diff (`git diff --cached --binary`)
- `_validate_patch(patch: str) -> None`: Validate patch (no `../` path traversal, size ≤ MAX_PATCH_SIZE)
- `_validate_clone_url(url: str) -> None`: Ensure HTTPS, no embedded credentials, valid host/path
- `_sanitize_url_for_log(url: str) -> str`: Remove credentials from URL
- `_run_git(repo_path: Path, *args: str, sanitize_token: str | None, timeout: int) -> str`: Run git command, sanitize errors

**Internal Imports**: `telemetry`

**Depended On By**: `gitlab_client.py`, `mr_comment_handler.py`, `coding_orchestrator.py`, `task_runner.py`, `coding_workflow.py`

---

### `comment_parser.py`
**Purpose**: Extract structured review output from Copilot agent response.

**Key Models**:
- `ReviewComment`: file, line, severity, comment, suggestion, suggestion_start_offset, suggestion_end_offset
- `ParsedReview`: comments, summary

**Key Functions**:
- `parse_review(raw: str) -> ParsedReview`: Extract JSON array from code fence or raw text, parse comments, extract summary

**Internal Imports**: None

**Depended On By**: `orchestrator.py`

---

### `comment_poster.py`
**Purpose**: Post review comments to GitLab MR as inline discussions and summary.

**Key Functions**:
- `post_review(gitlab_client: gl.Gitlab, project_id: int, mr_iid: int, diff_refs: MRDiffRef, review: ParsedReview, changes: list[MRChange]) -> None`: Post inline + summary
  - Validates comment positions against diff hunks
  - Falls back to note with file:line context if position invalid
- `_parse_hunk_lines(diff: str, new_path: str) -> set[tuple[str, int]]`: Extract valid (file, line) positions from unified diff
- `_is_valid_position(file: str, line: int, valid_positions: set[tuple[str, int]]) -> bool`: Check if position valid

**Internal Imports**: `comment_parser`, `gitlab_client`

**Depended On By**: `orchestrator.py`

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

**Depended On By**: `webhook.py`, `gitlab_poller.py`, `mr_comment_handler.py`, `orchestrator.py`

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

**Depended On By**: `jira_client.py`, `jira_poller.py`, `coding_orchestrator.py`

---

### `project_mapping.py`
**Purpose**: Jira project key → GitLab project mapping.

**Key Models**:
- `GitLabProjectMapping`: gitlab_project_id, clone_url, target_branch
- `ProjectMap`: mappings (dict of key → mapping)
  - `get(jira_project_key: str) -> GitLabProjectMapping | None`

**Internal Imports**: None

**Depended On By**: `jira_poller.py`, `coding_orchestrator.py`, `main.py`

---

### `config.py`
**Purpose**: Application configuration via environment variables.

**Key Models**:
- `JiraSettings`: url, email, api_token, trigger_status, in_progress_status, poll_interval, project_map_json
- `Settings` (BaseSettings): All env vars (see configuration-reference.md)
  - `jira` property: return JiraSettings if all required fields set, else None
  - `_check_auth()` validator: ensure either GITHUB_TOKEN or COPILOT_PROVIDER_TYPE set; validate REDIS_URL if backend=redis; validate GITLAB_PROJECTS if gitlab_poll=true

**Internal Imports**: None

**Depended On By**: All modules

---

## State & Concurrency

### `concurrency.py`
**Purpose**: In-memory locking, deduplication, and tracking.

**Key Protocols**:
- `DistributedLock`: `acquire(key: str, ttl_seconds: int) -> AbstractAsyncContextManager[None]`, `aclose()`
- `DeduplicationStore`: `is_seen(key: str) -> bool`, `mark_seen(key: str, ttl_seconds: int) -> None`, `aclose()`

**Key Classes**:
- `MemoryLock`: Async lock per key with LRU eviction
  - `acquire(key: str, ttl_seconds: int)`: Context manager, evicts unlocked entries after release
- `MemoryDedup`: In-memory seen set with size-based eviction
  - `is_seen(key: str) -> bool`, `mark_seen(key: str, ttl_seconds: int) -> None`
- `ProcessedIssueTracker`: Track processed Jira issue keys (LRU eviction)
- `ReviewedMRTracker`: Track reviewed (project_id, mr_iid, head_sha) tuples (LRU eviction)

**Aliases**:
- `RepoLockManager = MemoryLock` (backward compatibility)

**Internal Imports**: None

**Depended On By**: `main.py`, `webhook.py`, `gitlab_poller.py`, `coding_orchestrator.py`, `mr_comment_handler.py`

---

### `redis_state.py`
**Purpose**: Redis-backed implementations for Lock and DeduplicationStore.

**Key Constants**:
- `_UNLOCK_SCRIPT`: Lua script for atomic lock release
- `_EXTEND_SCRIPT`: Lua script for atomic TTL extension
- `_LOCK_RETRY_DELAY = 0.1`
- `_LOCK_PREFIX = "lock:"`
- `_DEDUP_PREFIX = "dedup:"`
- `_RENEWAL_FACTOR = 0.5`

**Key Classes**:
- `RedisLock`: Distributed lock using SET NX + TTL (Redlock-style)
  - `acquire(key: str, ttl_seconds: int)`: Spin until SET NX succeeds, start renewal loop
  - `_renew_loop(lock_key: str, token: str, ttl_seconds: int)`: Periodic EXPIRE via Lua script
  - `aclose()`: Close Redis connection
- `RedisDedup`: Redis-backed seen set
  - `is_seen(key: str) -> bool`: EXISTS check
  - `mark_seen(key: str, ttl_seconds: int) -> None`: SET with TTL
  - `aclose()`: Close Redis connection

**Key Functions**:
- `create_lock(backend: str, redis_url: str | None) -> DistributedLock`: Factory
- `create_dedup(backend: str, redis_url: str | None) -> DeduplicationStore`: Factory

**Internal Imports**: `concurrency`

**Depended On By**: `main.py`

---

## Telemetry

### `telemetry.py`
**Purpose**: OpenTelemetry tracing, metrics, and log export setup.

**Key Constants**:
- `_SERVICE_NAME = "gitlab-copilot-agent"`

**Key Functions**:
- `init_telemetry() -> None`: Configure OTEL providers, exporters, auto-instrumentation (FastAPI, httpx)
  - No-op if OTEL_EXPORTER_OTLP_ENDPOINT unset
  - Sets up TracerProvider, MeterProvider, LoggerProvider
  - Exports via OTLP gRPC
- `shutdown_telemetry() -> None`: Flush and shutdown providers
- `get_tracer(name: str) -> trace.Tracer`: Get tracer instance
- `add_trace_context(logger, method, event_dict) -> dict`: Structlog processor injecting trace_id, span_id
- `emit_to_otel_logs(logger, method, event_dict) -> dict`: Structlog processor re-emitting to stdlib logging for OTLP export

**Internal Imports**: None

**Depended On By**: `main.py`, `orchestrator.py`, `mr_comment_handler.py`, `coding_orchestrator.py`, `copilot_session.py`, `git_operations.py`, `jira_poller.py`

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

**Depended On By**: `orchestrator.py`, `coding_orchestrator.py`, `copilot_session.py`, `webhook.py`

---

### `process_sandbox.py`
**Purpose**: Copilot CLI binary resolution.

**Key Functions**:
- `_get_real_cli_path() -> str`: Resolve bundled Copilot CLI binary path from github-copilot-sdk package

**Internal Imports**: None

**Depended On By**: `copilot_session.py`

---

## Summary Table

| Module | Layer | LOC (approx) | Key Responsibility |
|--------|-------|--------------|---------------------|
| `main.py` | Ingestion | 168 | FastAPI app, lifespan, pollers |
| `webhook.py` | Ingestion | 117 | Webhook endpoint, HMAC validation |
| `gitlab_poller.py` | Ingestion | 175 | MR/note discovery |
| `jira_poller.py` | Ingestion | 90 | Issue discovery |
| `orchestrator.py` | Processing | 95 | MR review pipeline |
| `mr_comment_handler.py` | Processing | 130 | /copilot command handling |
| `coding_orchestrator.py` | Processing | 142 | Jira task implementation |
| `review_engine.py` | Processing | 91 | Review prompt construction |
| `coding_engine.py` | Processing | 109 | Coding prompt construction |
| `task_executor.py` | Execution | 53 | TaskExecutor protocol |
| `k8s_executor.py` | Execution | 219 | K8s Job orchestration |
| `copilot_session.py` | Execution | 143 | SDK wrapper |
| `task_runner.py` | Execution | 134 | K8s Job entrypoint |
| `gitlab_client.py` | Clients | 224 | GitLab API client |
| `jira_client.py` | Clients | 117 | Jira API client |
| `git_operations.py` | Utils | 194 | Git CLI wrappers |
| `comment_parser.py` | Utils | 75 | Review parsing |
| `comment_poster.py` | Utils | 137 | Comment posting |
| `repo_config.py` | Utils | 184 | Repo config discovery |
| `models.py` | Data | 79 | Webhook models |
| `jira_models.py` | Data | 87 | Jira API models |
| `project_mapping.py` | Data | 34 | Jira→GitLab mapping |
| `config.py` | Data | 137 | Settings |
| `concurrency.py` | State | 208 | In-memory locks/dedup |
| `redis_state.py` | State | 128 | Redis locks/dedup |
| `telemetry.py` | Telemetry | 130 | OTEL setup |
| `metrics.py` | Telemetry | 52 | Metrics instruments |
| `process_sandbox.py` | Utils | 20 | CLI path resolution |

**Total: 29 modules, ~3,270 lines of code**
