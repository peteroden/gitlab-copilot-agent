# Request Flows

End-to-end data flows with sequence diagrams for each major operation.

---

## 1. Webhook MR Review

Triggered when GitLab sends `merge_request` webhook with action `open` or `update` (with new commits).

```mermaid
sequenceDiagram
    participant GL as GitLab
    participant WH as webhook.py
    participant ORCH as orchestrator.py
    participant GLCL as gitlab_client.py
    participant EXEC as TaskExecutor
    participant COP as copilot_session.py
    participant SDK as Copilot SDK
    participant PARSE as comment_parser.py
    participant POST as comment_poster.py
    
    GL->>WH: POST /webhook (X-Gitlab-Token header)
    WH->>WH: _validate_webhook_token() (HMAC)
    alt Invalid token
        WH-->>GL: 401 Unauthorized
    end
    
    WH->>WH: Parse MergeRequestWebhookPayload
    WH->>WH: Check project allowlist
    WH->>WH: Check action in HANDLED_ACTIONS
    WH->>WH: Check oldrev (skip if None)
    WH->>WH: Check ReviewedMRTracker (skip if seen)
    
    WH->>WH: Add _process_review to background_tasks
    WH-->>GL: 200 {"status": "queued"}
    
    Note over WH,POST: Background task starts
    
    WH->>ORCH: handle_review(settings, payload, executor)
    ORCH->>GLCL: clone_repo(git_http_url, source_branch, token)
    GLCL->>GLCL: git_operations.git_clone()
    GLCL-->>ORCH: repo_path: Path
    
    ORCH->>EXEC: execute(TaskParams: review)
    alt LocalTaskExecutor
        EXEC->>COP: run_copilot_session(repo_path, prompts)
        COP->>SDK: CopilotClient.create_session()
        COP->>SDK: session.send({"prompt": user_prompt})
        SDK-->>COP: assistant.message events
        COP-->>EXEC: raw_review: str
    else KubernetesTaskExecutor
        EXEC->>EXEC: create_namespaced_job()
        Note over EXEC: Job runs task_runner.py
        EXEC->>EXEC: _wait_for_result (poll Redis)
        EXEC-->>EXEC: result from Redis
    end
    EXEC-->>ORCH: raw_review: TaskResult
    
    ORCH->>PARSE: parse_review(raw_review)
    PARSE-->>ORCH: ParsedReview (comments, summary)
    
    ORCH->>GLCL: get_mr_details(project_id, mr_iid)
    GLCL-->>ORCH: MRDetails (diff_refs, changes)
    
    ORCH->>POST: post_review(gl, project_id, mr_iid, diff_refs, parsed, changes)
    POST->>POST: _parse_hunk_lines() for all changes
    loop For each comment
        POST->>POST: _is_valid_position()
        alt Valid position
            POST->>GL: mr.discussions.create() (inline)
        else Invalid position
            POST->>GL: mr.notes.create() (fallback)
        end
    end
    POST->>GL: mr.notes.create() (summary)
    POST-->>ORCH: Done
    
    ORCH->>GLCL: cleanup(repo_path)
    ORCH->>ORCH: Mark (project_id, mr_iid, head_sha) as reviewed
    ORCH->>ORCH: Emit metrics (reviews_total, reviews_duration)
```

**Error Handling**:
- On exception in background task: log, emit `webhook_errors_total`, post failure comment to MR, re-raise
- Cleanup: `repo_path` removed in finally block

---

## 2. Webhook /copilot Command

Triggered when GitLab sends `note` webhook for MR comment starting with `/copilot `.

```mermaid
sequenceDiagram
    participant GL as GitLab
    participant WH as webhook.py
    participant MRC as mr_comment_handler.py
    participant GLCL as gitlab_client.py
    participant EXEC as TaskExecutor
    participant COP as copilot_session.py
    participant GIT as git_operations.py
    
    GL->>WH: POST /webhook (note event)
    WH->>WH: _validate_webhook_token()
    WH->>WH: Parse NoteWebhookPayload
    WH->>WH: Check noteable_type == "MergeRequest"
    WH->>WH: parse_copilot_command(note.body)
    alt Not a /copilot command
        WH-->>GL: 200 {"status": "ignored"}
    end
    WH->>WH: Check agent_gitlab_username (skip self)
    WH->>WH: Add _process_copilot_comment to background_tasks
    WH-->>GL: 200 {"status": "queued"}
    
    Note over WH,GIT: Background task starts
    
    WH->>MRC: handle_copilot_comment(settings, payload, executor, repo_locks)
    alt repo_locks provided
        MRC->>MRC: async with repo_locks.acquire(git_http_url)
    end
    
    MRC->>GIT: git_clone(git_http_url, source_branch, token)
    GIT-->>MRC: repo_path: Path
    
    MRC->>EXEC: execute(TaskParams: coding, prompt=instruction)
    EXEC->>COP: run_copilot_session(repo_path, get_prompt(settings, "coding"), instruction)
    COP-->>EXEC: result: TaskResult
    EXEC-->>MRC: result: TaskResult
    
    MRC->>MRC: apply_coding_result(result, repo_path)
    Note over MRC: If K8s: validate base_sha, git apply --3way<br/>If Local: no-op (files on disk)
    
    MRC->>GIT: git_commit(repo_path, message, author)
    alt has_changes
        MRC->>GIT: git_push(repo_path, "origin", source_branch, token)
        MRC->>GLCL: post_mr_comment(project_id, mr_iid, "✅ Changes pushed")
    else no changes
        MRC->>GLCL: post_mr_comment(project_id, mr_iid, "ℹ️ No file changes needed")
    end
    
    MRC->>MRC: shutil.rmtree(repo_path)
```

**Error Handling**:
- On exception: log, emit `webhook_errors_total`, post "❌ Agent encountered an error" comment, re-raise
- Cleanup: `repo_path` removed in finally block

---

## 3. GitLab Poller MR Discovery

Background poller discovers new/updated MRs via GitLab API.

```mermaid
sequenceDiagram
    participant POLL as gitlab_poller.py
    participant GLCL as gitlab_client.py
    participant DEDUP as DeduplicationStore
    participant ORCH as orchestrator.py
    
    loop Every interval
        POLL->>POLL: _poll_once()
        Note over POLL: poll_start = now()
        
        loop For each project_id
            POLL->>GLCL: list_project_mrs(project_id, state="opened", updated_after=watermark)
            GLCL-->>POLL: list[MRListItem]
            
            loop For each mr
                POLL->>POLL: Build dedup key: "review:{project_id}:{mr.iid}:{mr.sha}"
                POLL->>DEDUP: is_seen(key)
                alt Already seen
                    DEDUP-->>POLL: True (skip)
                else New
                    DEDUP-->>POLL: False
                    POLL->>POLL: Synthesize MergeRequestWebhookPayload
                    POLL->>ORCH: handle_review(settings, payload, executor)
                    Note over ORCH: Same flow as webhook review
                    ORCH-->>POLL: Done
                    POLL->>DEDUP: mark_seen(key, ttl=86400)
                end
            end
        end
        
        POLL->>POLL: watermark = poll_start
        POLL->>POLL: Sleep interval (or backoff if error)
    end
```

**Watermark Strategy**:
- `_watermark` initialized to `now()` on first start (avoids replaying historical notes)
- Updated to poll cycle start time after all projects processed
- `updated_after` filter ensures only MRs updated since last cycle are returned

**Dedup Key**: `review:{project_id}:{mr_iid}:{mr_sha}` (TTL: 24 hours)

**Error Handling**:
- On exception in `_poll_once()`: log, increment `_failures`, exponential backoff (max 300s)
- Successful cycle: reset `_failures` to 0

---

## 4. GitLab Poller Note Discovery

Background poller discovers `/copilot` notes on open MRs.

```mermaid
sequenceDiagram
    participant POLL as gitlab_poller.py
    participant GLCL as gitlab_client.py
    participant DEDUP as DeduplicationStore
    participant MRC as mr_comment_handler.py
    
    loop For each project_id
        POLL->>GLCL: list_project_mrs(project_id, state="opened", updated_after=watermark)
        GLCL-->>POLL: list[MRListItem]
        
        loop For each mr
            POLL->>GLCL: list_mr_notes(project_id, mr.iid, created_after=watermark)
            GLCL-->>POLL: list[NoteListItem]
            
            loop For each note
                alt note.system
                    Note over POLL: Skip system notes
                end
                POLL->>POLL: parse_copilot_command(note.body)
                alt Not a /copilot command
                    Note over POLL: Skip
                end
                POLL->>POLL: Check agent_gitlab_username (skip self)
                POLL->>POLL: Build dedup key: "note:{project_id}:{mr.iid}:{note.id}"
                POLL->>DEDUP: is_seen(key)
                alt Already seen
                    DEDUP-->>POLL: True (skip)
                else New
                    DEDUP-->>POLL: False
                    POLL->>POLL: Synthesize NoteWebhookPayload
                    POLL->>MRC: handle_copilot_comment(settings, payload, executor, repo_locks)
                    Note over MRC: Same flow as webhook command
                    MRC-->>POLL: Done
                    POLL->>DEDUP: mark_seen(key, ttl=86400)
                end
            end
        end
    end
```

**Dedup Key**: `note:{project_id}:{mr_iid}:{note.id}` (TTL: 24 hours)

**Self-Comment Guard**: If `agent_gitlab_username` is set and matches note author, skip processing (prevents infinite loop if agent posts `/copilot` command).

---

## 5. Jira Poller Coding Task

Background poller discovers Jira issues in "AI Ready" status.

```mermaid
sequenceDiagram
    participant POLL as jira_poller.py
    participant JCL as jira_client.py
    participant CODING as coding_orchestrator.py
    participant GIT as git_operations.py
    participant EXEC as TaskExecutor
    participant COP as copilot_session.py
    participant GLCL as gitlab_client.py
    
    loop Every interval
        POLL->>POLL: _poll_once()
        POLL->>POLL: Build JQL: status = "AI Ready" AND project IN (...)
        POLL->>JCL: search_issues(jql)
        JCL-->>POLL: list[JiraIssue]
        
        loop For each issue
            alt issue.key in _processed_issues
                Note over POLL: Skip already processed
            end
            
            POLL->>POLL: project_map.get(issue.project_key)
            alt No mapping
                Note over POLL: Skip
            end
            
            POLL->>CODING: handle(issue, project_mapping)
            
            Note over CODING: Acquire lock on clone_url
            
            CODING->>JCL: transition_issue(issue.key, "In Progress")
            CODING->>GIT: git_clone(clone_url, target_branch, token)
            GIT-->>CODING: repo_path: Path
            
            CODING->>GIT: git_unique_branch(repo_path, "agent/{issue-key}")
            Note over GIT: Checks remote refs via git ls-remote<br/>Appends -2, -3 on collision
            
            CODING->>EXEC: execute(TaskParams: coding, Jira prompt)
            EXEC->>COP: run_copilot_session(repo_path, get_prompt(settings, "coding"), jira_prompt)
            COP-->>EXEC: result: TaskResult
            EXEC-->>CODING: result: TaskResult
            
            CODING->>CODING: apply_coding_result(result, repo_path)
            Note over CODING: If K8s: validate base_sha, git apply --3way<br/>If Local: no-op (files on disk)
            
            CODING->>GIT: git_commit(repo_path, message, author)
            alt has_changes
                CODING->>GIT: git_push(repo_path, "origin", branch, token)
                CODING->>GLCL: create_merge_request(...)
                GLCL-->>CODING: mr_iid: int
                CODING->>JCL: add_comment(issue.key, "MR created: {url}")
                CODING->>CODING: tracker.mark(issue.key)
                CODING->>CODING: Emit metrics (coding_tasks_total: success)
            else no changes
                CODING->>JCL: add_comment(issue.key, "Agent found no changes to make")
                CODING->>CODING: Emit metrics (coding_tasks_total: no_changes)
            end
            
            CODING->>CODING: shutil.rmtree(repo_path)
            CODING-->>POLL: Done
            
            POLL->>POLL: _processed_issues.add(issue.key)
        end
        
        POLL->>POLL: Sleep interval
    end
```

**Processed Tracker**: In-memory set of processed issue keys (cleared on service restart).

**Locking**: Per-repo lock on `clone_url` to prevent concurrent clone/push operations.

**Error Handling**:
- On exception: log, emit `coding_tasks_total` with outcome=error, post failure comment to Jira, re-raise
- Cleanup: `repo_path` removed in finally block

---

## Error Handling Patterns

### Webhook Background Tasks
- Exception logged with `aexception()` (includes stack trace)
- Metrics emitted: `webhook_errors_total` with label `handler="review"` or `handler="copilot_comment"`
- Failure comment posted to MR (best effort, secondary exception logged but swallowed)
- Exception re-raised (captured by FastAPI, returns 500 to webhook sender only if still in request context)

### Poller Tasks
- Exception logged with `aexception()`
- Failure counter incremented: `_failures += 1`
- Exponential backoff: `sleep(min(interval * 2**failures, 300))`
- Successful cycle resets `_failures = 0`

### Git Operations
- Token sanitized in error messages (replaced with `***`)
- Timeout enforced (120s for clone, 60s for other ops)
- Cleanup on error: `shutil.rmtree(tmp_dir, ignore_errors=True)` in clone

### Task Execution
- LocalTaskExecutor: exceptions propagate to caller
- KubernetesTaskExecutor:
  - Job timeout: delete Job, raise TimeoutError
  - Job failure: read pod logs, delete Job, raise RuntimeError with logs
  - Redis unavailable: exception propagates

### Copilot Session
- Timeout enforced via `asyncio.wait_for(done.wait(), timeout=timeout)`
- Session destroyed in finally block
- CLI path validation (raises if binary not found)

---

## Sequence Diagram Legend

- **Solid arrow (`->>`)**: Synchronous call or message
- **Dashed arrow (`-->>`)**: Return value or response
- **Alt block**: Conditional branching
- **Loop block**: Iteration
- **Note**: Clarifying comment

---

## Performance Characteristics

| Flow | Latency | Bottleneck | Parallelism |
|------|---------|-----------|-------------|
| Webhook MR review | 30-120s | Copilot SDK session | None (sequential: clone → review → post) |
| Webhook /copilot | 30-120s | Copilot SDK session | Per-repo lock (serializes concurrent commands on same MR) |
| GitLab poller (MR) | Poll interval + review latency | GitLab API rate limits | Sequential per project |
| GitLab poller (notes) | Poll interval + command latency | GitLab API rate limits | Sequential per project |
| Jira poller | Poll interval + coding latency | Jira API rate limits | Sequential per issue, per-repo lock |

**Copilot Session Timeout**: Default 300s (5 minutes), configurable per-call.

**GitLab API Rate Limits**: python-gitlab client retries on 429, respects Retry-After header.
