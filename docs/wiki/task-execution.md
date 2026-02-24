# Task Execution

TaskExecutor protocol, LocalTaskExecutor vs KubernetesTaskExecutor, prompt construction, Copilot SDK integration.

---

## TaskExecutor Protocol

**Interface**:
```python
@runtime_checkable
class TaskExecutor(Protocol):
    async def execute(self, task: TaskParams) -> TaskResult: ...
```

**TaskParams**:
- `task_type: Literal["review", "coding"]` — Type of task
- `task_id: str` — Unique identifier (for idempotency, result caching)
- `repo_url: str` — Git clone URL
- `branch: str` — Branch to work on
- `system_prompt: str` — System prompt for Copilot session
- `user_prompt: str` — User prompt for Copilot session
- `settings: Settings` — Application configuration
- `repo_path: str | None` — Local path to cloned repo (required for LocalTaskExecutor)

**Return**: `TaskResult` — union type `ReviewResult | CodingResult` (Pydantic models, frozen=True)
- `ReviewResult`: `summary: str` — plain text summary
- `CodingResult`: `summary: str`, `patch: str` (unified diff, binary-safe), `base_sha: str` (commit SHA at time of capture)

---

## LocalTaskExecutor

**Purpose**: Runs Copilot sessions directly in-process (single-pod, dev/test).

**Implementation**:
```python
class LocalTaskExecutor:
    async def execute(self, task: TaskParams) -> TaskResult:
        if not task.repo_path:
            raise ValueError("LocalTaskExecutor requires task.repo_path")
        
        summary = await run_copilot_session(
            settings=task.settings,
            repo_path=task.repo_path,
            system_prompt=task.system_prompt,
            user_prompt=task.user_prompt,
            task_type=task.task_type,
        )
        
        # Return appropriate result type
        if task.task_type == "review":
            return ReviewResult(summary=summary)
        else:
            return CodingResult(summary=summary, patch="", base_sha="")
```

**Requires**: `task.repo_path` must be set (caller clones repo before execution).

**Note**: LocalTaskExecutor returns `ReviewResult` for reviews, `CodingResult` with empty patch for coding tasks (files modified on disk directly, no diff passback needed).

**Isolation**: None (SDK subprocess shares UID, filesystem, network namespace).

**Timeout**: Configurable per-call (default: 300s).

---

## KubernetesTaskExecutor

**Purpose**: Dispatches tasks as ephemeral K8s Jobs (multi-pod, production).

**Flow**:
1. Check Redis for cached result (idempotency)
2. Build Job name: `copilot-{task_type}-{hash(task_id)[:16]}` (max 63 chars)
3. Create Job with `task_runner.py` as entrypoint
4. Poll Job status + Redis for result
5. Return result or raise exception

**Job Spec**:
- **Image**: `settings.k8s_job_image`
- **Command**: `[".venv/bin/python", "-m", "gitlab_copilot_agent.task_runner"]`
- **Env Vars**: TASK_TYPE, TASK_ID, REPO_URL, BRANCH, TASK_PAYLOAD, GITLAB_TOKEN, GITHUB_TOKEN, REDIS_URL, HOME=/tmp (Copilot CLI requires writable HOME)
- **hostAliases**: Optional JSON-encoded array from `K8S_JOB_HOST_ALIASES` for custom DNS (air-gapped, k3d dev)
- **Resources**: CPU/memory limits from settings
- **SecurityContext**: `runAsNonRoot`, `readOnlyRootFilesystem`, `capabilities.drop: ["ALL"]`
- **TTL**: `ttl_seconds_after_finished=300` (auto-delete after 5 minutes)

**Polling**:
- Interval: 2 seconds
- Timeout: `settings.k8s_job_timeout` (default: 600s)
- Checks: Redis first (result cached), then Job status

**Result Storage**:
- **Redis Key**: `result:{task_id}`
- **TTL**: 3600s (1 hour)
- **Written By**: `task_runner.py` (Job pod)
- **Read By**: `KubernetesTaskExecutor._wait_for_result()`

**Failure Handling**:
- Job succeeded but no result in Redis: read annotation `results.copilot-agent/summary` (fallback)
- Job failed: read pod logs, delete Job, raise RuntimeError with logs
- Timeout: delete Job, raise TimeoutError

**Idempotency & Stale Job Replacement**:
- Check Redis before creating Job
- If Job already exists (409 Conflict):
  - **Completed** (succeeded/failed): delete stale Job and create a fresh one
  - **Running**: reuse the existing Job (continue to polling)
- Prevents stale completed Jobs from previous runs returning empty results on retry

---

## task_runner.py (K8s Job Entrypoint)

**Purpose**: Standalone script that runs inside K8s Job pod, executes Copilot session, stores result in Redis.

**Flow**:
1. Read env vars: TASK_TYPE, TASK_ID, REPO_URL, BRANCH, TASK_PAYLOAD
2. Validate TASK_TYPE ∈ {"review", "coding", "echo"}
3. Validate REPO_URL authority matches GITLAB_URL (host + port)
4. Clone repo via `git_clone()`
5. Call `run_copilot_session()` with appropriate system prompt
   - **For coding tasks**: includes `validate_response` callback that checks for required JSON output; retries once in-session if missing
6. **For coding tasks**: Parse structured output and stage files:
   - Parse agent response as `CodingAgentOutput` (Pydantic model with `summary` and `files_changed`)
   - Stage only explicitly listed files via `git add -- <file>` (not `git add -A`)
   - Skip files that don't exist on disk (logged as warnings)
   - `git rev-parse HEAD` (capture base_sha)
   - `git diff --cached --binary --no-pager` (capture unified diff, preserving trailing whitespace)
   - Validate patch size ≤ `MAX_PATCH_SIZE` (10 MB, from `git_operations.py`)
   - Validate patch for path traversal (`../`)
   - Build `CodingResult(summary, patch, base_sha)`
7. **For review tasks**: Build `ReviewResult(summary)`
8. Store result in Redis (`result:{task_id}`, TTL=3600s) as JSON
9. Print JSON result to stdout (for debugging)
10. Return exit code 0 (success) or 1 (error)

**Validation**: `_validate_repo_url()` ensures REPO_URL is from trusted GitLab instance (prevents SSRF).

**Example**:
```python
$ python -m gitlab_copilot_agent.task_runner
# Reads TASK_TYPE=review, TASK_ID=review-feature-x, REPO_URL=https://gitlab.com/group/proj.git
# Clones repo, runs review, stores result in Redis
```

---

## Prompt Construction

### System Prompts

#### REVIEW_SYSTEM_PROMPT (`review_engine.py`)
```
You are a senior code reviewer. Review the merge request diff thoroughly.

Focus on:
- Bugs, logic errors, and edge cases
- Security vulnerabilities (OWASP Top 10)
- Performance issues
- Code clarity and maintainability

You have access to the full repository via built-in file tools. Use them to
read source files and understand context beyond the diff.

Output your review as a JSON array:
[
  {
    "file": "path/to/file",
    "line": 42,
    "severity": "error|warning|info",
    "comment": "Description of the issue",
    "suggestion": "replacement code for the line(s)",
    "suggestion_start_offset": 0,
    "suggestion_end_offset": 0
  }
]

After the JSON array, add a brief summary paragraph.
If the code looks good, return an empty array and say so in the summary.
```

#### CODING_SYSTEM_PROMPT (`coding_engine.py`)
```
You are a senior software engineer implementing requested changes.

Your workflow:
1. Read the task description carefully to understand requirements
2. Explore the existing codebase using file tools to understand structure and conventions
3. Make minimal, focused changes that address the task
4. Follow existing project conventions (code style, patterns, architecture)
5. Ensure .gitignore exists with standard ignores for the project language
6. Run the project linter if available and fix any issues
7. Run tests if available to verify your changes
8. Output a summary of changes made

Guidelines:
- Make the smallest change that solves the problem
- Preserve existing behavior unless explicitly required to change it
- Follow SOLID principles and existing patterns
- Add tests for new functionality
- Update documentation if needed
- Do not introduce new dependencies without strong justification
- Never commit generated or cached files (__pycache__, .pyc, node_modules, etc.)
```

---

### User Prompts

#### Review Prompt (`review_engine.py`)
```python
def build_review_prompt(req: ReviewRequest) -> str:
    return (
        f"## Merge Request\n"
        f"**Title:** {req.title}\n"
        f"**Description:** {req.description or '(none)'}\n"
        f"**Source branch:** {req.source_branch}\n"
        f"**Target branch:** {req.target_branch}\n\n"
        f"Review this merge request. Run "
        f"`git diff {req.target_branch}...{req.source_branch}` to see "
        f"the changes, then read relevant files for context."
    )
```

#### Coding Prompt (Jira, `coding_engine.py`)
```python
def build_jira_coding_prompt(issue_key: str, summary: str, description: str | None) -> str:
    desc_text = description if description else "(no description provided)"
    return (
        f"## Jira Issue: {issue_key}\n"
        f"**Summary:** {summary}\n"
        f"**Description:**\n{desc_text}\n\n"
        f"Implement the changes described in this issue. "
        f"Explore the repository, make necessary changes, run tests, "
        f"and provide a summary of what you did."
    )
```

#### Coding Prompt (MR Comment, `mr_comment_handler.py`)
```python
def build_mr_coding_prompt(instruction: str, mr_title: str, source_branch: str, target_branch: str) -> str:
    return (
        f"## MR: {mr_title}\n"
        f"**Branch:** {source_branch} → {target_branch}\n"
        f"**Instruction:** {instruction}\n\n"
        f"Implement the requested changes on this merge request. "
        f"Explore the repository, make the changes, run tests, "
        f"and provide a summary of what you did."
    )
```

---

### Repo Config Injection

**Location**: `copilot_session.py` → `run_copilot_session()`

**Discovery**:
1. Call `discover_repo_config(repo_path)` → returns `RepoConfig`
2. Extract skills, agents, instructions

**Injection**:
```python
system_content = system_prompt
if repo_config.instructions:
    system_content += (
        f"\n\n## Project-Specific Instructions\n\n{repo_config.instructions}\n"
    )
```

**Example**: If `.github/copilot-instructions.md` contains project conventions, they're appended to system prompt.

**Agent Config**:
```python
if repo_config.custom_agents:
    session_opts["custom_agents"] = [
        cast(CustomAgentConfig, a.model_dump(exclude_none=True))
        for a in repo_config.custom_agents
    ]
```

**Skills**:
```python
if repo_config.skill_directories:
    session_opts["skill_directories"] = repo_config.skill_directories
```

---

## Copilot Session Lifecycle

**Location**: `copilot_session.py` → `run_copilot_session()`

**Steps**:
1. **Resolve CLI Path**: `_get_real_cli_path()` → find bundled Copilot CLI binary
2. **Build SDK Env**: `build_sdk_env(github_token)` → minimal env dict (excludes service secrets)
3. **Create Client**:
   ```python
   client_opts: CopilotClientOptions = {
       "cli_path": cli_path,
       "env": build_sdk_env(settings.github_token),
   }
   if settings.github_token:
       client_opts["github_token"] = settings.github_token
   client = CopilotClient(client_opts)
   await client.start()
   ```
4. **Discover Repo Config**: `discover_repo_config(repo_path)` → skills, agents, instructions
5. **Inject Instructions**: Append to system prompt
6. **Build Session Options**:
   ```python
   session_opts: SessionConfig = {
       "system_message": {"content": system_content},
       "working_directory": repo_path,
   }
   if repo_config.skill_directories:
       session_opts["skill_directories"] = repo_config.skill_directories
   if repo_config.custom_agents:
       session_opts["custom_agents"] = [...]
   ```
7. **BYOK Provider** (if configured):
   ```python
   if settings.copilot_provider_type:
       provider: ProviderConfig = {
           "type": settings.copilot_provider_type,
       }
       if settings.copilot_provider_base_url:
           provider["base_url"] = settings.copilot_provider_base_url
       if settings.copilot_provider_api_key:
           provider["api_key"] = settings.copilot_provider_api_key
       session_opts["provider"] = provider
       session_opts["model"] = settings.copilot_model
   ```
8. **Create Session**: `await client.create_session(session_opts)`
9. **Send Prompt**: `await session.send({"prompt": user_prompt})`
10. **Wait for Idle**:
    ```python
    done = asyncio.Event()
    messages: list[str] = []
    
    def on_event(event: Any) -> None:
        match getattr(event, "type", None):
            case t if t and t.value == "assistant.message":
                content = getattr(event.data, "content", "")
                if content:
                    messages.append(content)
            case t if t and t.value == "session.idle":
                done.set()
    
    session.on(on_event)
    await asyncio.wait_for(done.wait(), timeout=timeout)
    ```
11. **Extract Result**: `result = messages[-1] if messages else ""`
12. **Destroy Session**: `await session.destroy()` (in finally)
13. **Stop Client**: `await client.stop()` (in finally)
14. **Emit Metric**: `copilot_session_duration.record(elapsed, {"task_type": task_type})`

**Timeout**: Default 300s (5 minutes), enforced by `asyncio.wait_for()`.

**Error Handling**: Session and client destroyed in finally blocks, exceptions propagate.

---

## SDK Environment Isolation

**Allowlist**: `_SDK_ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER"})`

**Excluded Secrets**:
- `GITLAB_TOKEN`
- `GITLAB_WEBHOOK_SECRET`
- `JIRA_API_TOKEN`
- `JIRA_EMAIL`
- `COPILOT_PROVIDER_API_KEY`

**Included**:
- `GITHUB_TOKEN` (required for Copilot SDK)
- Standard env vars (PATH, HOME, etc.)

**Rationale**: Minimize SDK subprocess access to service secrets. If SDK is compromised (e.g., via prompt injection), attacker cannot exfiltrate GitLab/Jira tokens.

---

## .gitignore Hygiene (`coding_engine.py`)

**Purpose**: Ensure cloned repos have standard `.gitignore` patterns before coding.

**Function**: `ensure_gitignore(repo_root: str) -> bool`

**Patterns** (Python-specific):
```python
_PYTHON_GITIGNORE_PATTERNS = [
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    "*.egg-info/",
    "dist/",
    "build/",
    ".venv/",
]
```

**Behavior**:
1. Check if `.gitignore` exists at repo root
2. Reject symlinks or paths escaping repo root
3. Read existing content
4. Add missing patterns
5. Return `True` if modified, `False` if no changes

**Security**: Refuses to write if `.gitignore` is a symlink or resolves outside repo root.

**Invocation**: Called before `run_coding_task()` → agent can safely stage all files without committing generated artifacts.

---

## Result Extraction

### Review Output (`comment_parser.py`)

**Input**: Raw agent response (markdown + JSON)

**Parsing**:
1. Extract JSON array via regex: `` ```json\n[...]\n``` `` or raw `[...]`
2. Parse JSON via `json.loads()`
3. Validate each comment dict (required: file, line, comment)
4. Extract summary (text after JSON block)
5. Fallback: If no JSON found, treat entire output as summary

**Output**: `ParsedReview` (comments: list[ReviewComment], summary: str)

---

### Coding Output

**K8s Executor**: `CodingResult` with `summary`, `patch` (unified diff), and `base_sha` (commit SHA)

**Local Executor**: `CodingResult` with `summary`, empty `patch` (files modified on disk directly)

**Patch Format** (K8s only):
- Unified diff from `git diff --cached --binary`
- Binary-safe (can include binary file changes)
- Validated for size (≤ 10 MB) and path traversal (`../`)

**Summary Format** (by convention, not enforced):
- List of modified/created files
- Key changes made
- Test results (if tests were run)
- Concerns or follow-up items

---

## Diff Passback (K8s Executor)

**Problem**: Coding tasks in K8s Jobs modify files inside ephemeral pod. When pod terminates, all changes are lost. The controller (which creates MR/commits) never sees modifications.

**Solution**: Job pod captures `git diff --cached --binary` after Copilot runs, stores `CodingResult{summary, patch, base_sha}` in Redis. Controller reads result, validates `base_sha` matches local HEAD, applies patch with `git apply --3way`, then commits/pushes.

**Flow**:
1. **Job pod**: Clone repo → Copilot session → `git add -A` → capture base_sha and diff → store `CodingResult` in Redis
2. **Controller**: Read `CodingResult` from Redis → validate `base_sha == local HEAD` → `git apply --3way <patch>` → commit → push

**Validation** (in `coding_workflow.py`):
- `base_sha` mismatch: raises error (prevents applying patch to wrong commit)
- Patch contains `../`: raises error (path traversal attack prevention)
- Patch exceeds `MAX_PATCH_SIZE` (10 MB): raises error (prevents Redis OOM)

**Helper Function**: `apply_coding_result(result: TaskResult, repo_path: Path)` in `coding_workflow.py`
- Called by `CodingOrchestrator` and `MRCommentHandler` after execution
- No-op if `result.patch` is empty (LocalTaskExecutor)
- Applies patch via `git_apply_patch()` from `git_operations.py`

**New Git Operations** (in `git_operations.py`):
- `git_apply_patch(repo_path, patch)`: Apply patch with `git apply --3way --binary`
- `git_head_sha(repo_path)`: Get current HEAD SHA
- `git_diff_staged(repo_path)`: Capture staged diff
- `_validate_patch(patch)`: Reject patches with `../` (internal)

---

## Comparison: Local vs K8s

| Feature | LocalTaskExecutor | KubernetesTaskExecutor |
|---------|-------------------|------------------------|
| **Deployment** | Single pod only | Multi-pod, horizontal scaling |
| **Isolation** | None (same process) | Pod-level (network, filesystem, process) |
| **Resource Limits** | None (host limits) | CPU/memory via K8s |
| **Repo Clone** | Caller clones | Job clones (task_runner.py) |
| **Coding Result** | Files on disk, empty patch | Diff captured in pod, patch stored in Redis |
| **Diff Handling** | N/A (caller sees changes) | `git apply --3way` by controller after result read |
| **Timeout** | Per-call (default 300s) | Per-Job (default 600s) |
| **Idempotency** | None (no result caching) | Redis result cache (1 hour TTL) |
| **Failure Recovery** | Exception propagates | Pod logs captured, Job deleted |
| **Concurrency** | Limited by pod resources | Unlimited (new Job per task) |
| **Cleanup** | Caller responsible | Job auto-deleted (TTL 300s) |
| **Security** | Same UID as service | Separate pod, runAsNonRoot |

**Recommendation**: Use LocalTaskExecutor for dev/test, KubernetesTaskExecutor for production.

---

## Metrics

### copilot_session_duration

**Type**: Histogram

**Unit**: Seconds

**Labels**:
- `task_type`: `"review"` or `"coding"`

**Emitted**: `copilot_session.py` → `run_copilot_session()` (finally block)

**Purpose**: Track SDK session latency (includes prompt, model inference, result extraction).

---

## Debugging

### Local Executor

**Logs**: structlog output from `copilot_session.py`, `review_engine.py`, `coding_engine.py`

**Trace**: OTEL spans: `copilot.session`, `mr.review`, `mr.copilot_command`, `jira.coding_task`

**Errors**: Exception stack traces in logs

---

### K8s Executor

**Logs**: 
- Service logs: Job creation, polling, result retrieval
- Job pod logs: `task_runner.py` output (structlog)

**Commands**:
```bash
# List recent jobs
kubectl get jobs --sort-by=.metadata.creationTimestamp

# View job pod logs
kubectl logs job/copilot-review-abc123

# Describe job (check failure reason)
kubectl describe job copilot-review-abc123
```

**Common Errors**:
- **ImagePullBackOff**: Invalid `K8S_JOB_IMAGE`
- **CrashLoopBackOff**: task_runner.py failed (check pod logs)
- **Timeout**: Job exceeded `K8S_JOB_TIMEOUT` (increase timeout or investigate slow operations)
- **Redis unavailable**: Result not stored (check Redis connectivity)

---

## Performance Tuning

**Timeout**: Increase `K8S_JOB_TIMEOUT` for large repos or complex tasks.

**Resources**: Adjust `K8S_JOB_CPU_LIMIT` and `K8S_JOB_MEMORY_LIMIT` based on profiling.

**Parallelism**: No limit on concurrent Jobs (K8s handles scheduling).

**Result Caching**: Redis TTL=3600s allows retrying failed operations without re-running agent.

**SDK Session**: Default timeout 300s (5 minutes). Agent often completes in 30-60s for typical reviews.
