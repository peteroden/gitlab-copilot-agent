# Concurrency & State

Distributed locking, deduplication stores, watermark strategy, and race condition prevention.

---

## Lock Protocol

### Interface: `DistributedLock`

```python
@runtime_checkable
class DistributedLock(Protocol):
    def acquire(self, key: str, ttl_seconds: int = 300) -> AbstractAsyncContextManager[None]: ...
    async def aclose(self) -> None: ...
```

**Usage**:
```python
async with repo_locks.acquire(git_http_url):
    # Exclusive access to repo
    await git_clone(...)
    await git_push(...)
```

**Implementations**:
- `MemoryLock` (`concurrency/memory.py`): In-process asyncio locks with LRU eviction (created via `state.create_lock()`)

---

## MemoryLock

**Purpose**: Per-key asyncio locks for single-pod deployments.

**Data Structure**: `OrderedDict[str, asyncio.Lock]` (LRU cache)

**Behavior**:
1. `acquire(key)`: Get or create lock for key, move to end (most recently used)
2. Acquire asyncio.Lock (async context manager)
3. On exit: Evict unlocked entries if size exceeds `max_size` (default: 1024)
4. Locked entries never evicted (prevents deadlock)

**Eviction**:
- Walks `OrderedDict` from oldest to newest
- Checks `lock.locked()` for each entry
- Evicts unlocked entries until size ≤ `max_size`
- Logs warning with evicted count

**Code**:
```python
@asynccontextmanager
async def acquire(self, key: str, ttl_seconds: int = 300) -> AsyncIterator[None]:
    if key not in self._locks:
        self._locks[key] = asyncio.Lock()
    else:
        self._locks.move_to_end(key)  # LRU
    
    async with self._locks[key]:
        yield
    
    self._evict_unlocked()  # After release
```

**Thread Safety**: Not thread-safe (asyncio single-threaded event loop).

**Persistence**: None (cleared on service restart).

---

## RedisLock

**Purpose**: Distributed lock for multi-pod deployments.

**Algorithm**: Redlock-style SET NX + TTL + token-based release.

**Behavior**:
1. Generate unique token (UUID)
2. Spin until `SET lock:{key} {token} NX EX {ttl}` succeeds
3. Start renewal loop: periodically extend TTL via Lua script
4. On exit: release lock via Lua script (only if we still own it)
5. Cancel renewal loop

**Key Format**: `lock:{key}` (e.g., `lock:https://gitlab.com/group/project.git`)

**Lua Scripts**:
- **Unlock**: `if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end`
- **Extend**: `if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('expire',KEYS[1],ARGV[2]) else return 0 end`

**Renewal**:
- Interval: `max(1, int(ttl * 0.5))` seconds (default: 150s for TTL=300)
- Prevents lock expiration during long operations (e.g., large repo clone)
- Stops on context exit or connection error

**Code**:
```python
@asynccontextmanager
async def acquire(self, key: str, ttl_seconds: int = 300) -> AsyncIterator[None]:
    lock_key = f"{_LOCK_PREFIX}{key}"
    token = uuid4().hex
    while not await self._client.set(lock_key, token, nx=True, ex=ttl_seconds):
        await asyncio.sleep(_LOCK_RETRY_DELAY)
    
    renewal_task = asyncio.create_task(self._renew_loop(lock_key, token, ttl_seconds))
    try:
        yield
    finally:
        renewal_task.cancel()
        await self._client.eval(_UNLOCK_SCRIPT, 1, lock_key, token)
```

**Failure Modes**:
- **Redis unavailable**: Spin forever (blocks all operations)
- **Network partition**: Lock may expire, renewal fails silently
- **Token mismatch**: Unlock no-op (lock released by someone else or expired)

**Single-Instance Limitation**: Not safe for multi-Redis setups (true Redlock requires quorum).

---

## What Gets Locked

**Lock Key**: `git_http_url` (e.g., `https://gitlab.com/group/project.git`)

**Operations Locked**:
1. **MR @mention interaction** (`discussion_orchestrator.py`):
   - Clone → code → commit → push
   - Prevents concurrent pushes to same branch
2. **Jira coding task** (`coding_orchestrator.py`):
   - Clone → branch → code → commit → push → create MR
   - Prevents concurrent branch creation

**Not Locked**:
- **MR review** (`orchestrator.py`): Read-only operation (clone, no push)
- **GitLab poller**: Uses dedup store instead (no write operations per se)

**Rationale**: Git operations on same repo must be serialized to avoid:
- Push conflicts (concurrent pushes to same branch)
- Branch name collisions (concurrent `agent/{issue-key}` creation)
- Clone failures (concurrent clones to same temp dir prefix)

---

## Deduplication Protocol

### Interface: `DeduplicationStore`

```python
@runtime_checkable
class DeduplicationStore(Protocol):
    async def is_seen(self, key: str) -> bool: ...
    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None: ...
    async def aclose(self) -> None: ...
```

**Usage**:
```python
key = f"review:{project_id}:{mr_iid}:{head_sha}"
if await dedup.is_seen(key):
    return  # Skip duplicate
await dedup.mark_seen(key, ttl_seconds=86400)
```

**Implementations**:
- `MemoryDedup` (`concurrency/memory.py`): In-process set with size-based eviction (created via `state.create_dedup()`)

---

## MemoryDedup

**Purpose**: In-memory deduplication for single-pod deployments.

**Data Structure**: `OrderedDict[str, None]` (ordered set)

**Behavior**:
1. `is_seen(key)`: Check if key in dict
2. `mark_seen(key, ttl)`: Add key to dict, evict if needed
3. Eviction: When size exceeds `max_size` (default: 10,000), evict oldest 50%

**Eviction**:
- Target size: `max_size // 2`
- Removes oldest entries via `popitem(last=False)`
- Logs warning with evicted count

**Thread Safety**: Not thread-safe (asyncio single-threaded).

**Persistence**: None (cleared on service restart).

**TTL Handling**: Ignored (no expiration, relies on eviction only).

---

## RedisDedup

**Purpose**: Distributed deduplication for multi-pod deployments.

**Key Format**: `dedup:{key}` (e.g., `dedup:review:42:7:abc123`)

**Behavior**:
1. `is_seen(key)`: `EXISTS dedup:{key}` → returns `True` if exists
2. `mark_seen(key, ttl)`: `SET dedup:{key} "1" EX {ttl}` → set with TTL
3. Redis auto-expires after TTL (default: 3600s = 1 hour)

**No Eviction**: Redis handles expiration via TTL.

**Persistence**: Survives pod restarts (until TTL expires).

---

## What Gets Deduplicated

### 1. MR Reviews (Webhook + Poller)

**Key**: `review:{project_id}:{mr_iid}:{head_sha}`

**TTL**: 
- Webhook: 86400s (24 hours, via `DeduplicationService.mark_review()`)
- Poller: 86400s (24 hours)

**Purpose**: Prevent duplicate reviews on same commit.

**Tracked By**:
- **Webhook**: `DeduplicationService.is_review_seen()` / `mark_review()` (backed by `DeduplicationStore`)
  - Key: `review:{project_id}:{mr_iid}:{head_sha}`
  - Checked before dispatching `_process_review()`, marked after success
- **Poller**: `DeduplicationStore` (memory or Redis)
  - Marked in `gitlab_poller.py` → `_process_mr()`

**Race Condition**: 
- **Webhook**: Multiple pods receive same webhook → each pod checks own `DeduplicationService` → duplicate reviews possible without shared dedup store
- **Poller**: Single poller instance → no race (watermark ensures MRs processed once per cycle)

**Mitigation**: Use Redis dedup store for webhook in multi-pod setup.

---

### 2. @mention Notes (Poller)

**Key**: `note:{project_id}:{mr_iid}:{note_id}`

**TTL**: 86400s (24 hours)

**Purpose**: Prevent re-processing same @mention comment.

**Tracked By**: `DeduplicationStore` in `gitlab_poller.py` → `_process_notes()`

**No Webhook Dedup**: Webhook handler does not dedupe (assumes GitLab sends each note event once).

---

### 3. Jira Issues (Poller Only)

**Key**: `issue:{issue_key}` (e.g., `"issue:PROJ-123"`)

**TTL**: 86400s (24 hours, via `DeduplicationService.mark_issue()`)

**Purpose**: Prevent re-processing same issue in a single poller run.

**Tracked By**: `DeduplicationService.is_issue_seen()` / `mark_issue()` in `jira_poller.py`

**Persistence**: Backed by `DeduplicationStore` (cleared on service restart for memory backend).

---

### 4. Incremental Review SHA Marker

**Key**: Hidden HTML comment in summary note body (`<!-- mr-review-agent: last_reviewed_sha={sha} -->`)

**Storage**: GitLab overview note (persisted by GitLab, not in-memory)

**Purpose**: Track the last-reviewed commit SHA for incremental diff computation.

**Mechanism**:
1. After each review, the SHA marker is embedded in the summary note via `comment_poster.py`
2. On subsequent reviews, `orchestrator.py` calls `extract_last_reviewed_sha()` to find the marker
3. If found, the Compare API computes the diff between the marker SHA and current head SHA
4. If not found (first review, deploy, or failed post), falls back to full MR diff

**Self-Healing**: Absent marker → full review (correct behavior for first review or post-deploy).

**Not a Dedup Mechanism**: The SHA marker does not prevent duplicate reviews — that's handled by `DeduplicationService` and `DeduplicationStore`. The marker only controls diff scope.

See ADR-0009 for the design decision.

---

## DeduplicationService

**Purpose**: Unified deduplication service that consolidates all dedup logic into a single interface (`dedup.py`). Replaces the former `ReviewedMRTracker` and `ProcessedIssueTracker` classes.

**Backed By**: `DeduplicationStore` (same protocol used for poller dedup)

**Methods**:
1. `is_review_seen(project_id, mr_iid, head_sha)`: Check if MR review already processed
2. `mark_review(project_id, mr_iid, head_sha)`: Mark MR review as processed
3. `is_note_seen(project_id, mr_iid, note_id)`: Check if note already processed
4. `mark_note(project_id, mr_iid, note_id)`: Mark note as processed
5. `is_issue_seen(issue_key)`: Check if Jira issue already processed
6. `mark_issue(issue_key)`: Mark Jira issue as processed

**Usage**: `webhook.py` checks `dedup.is_review_seen()` before dispatching `_process_review()`, marks after success. `jira_poller.py` checks `dedup.is_issue_seen()` before dispatching coding tasks.

**Advantage Over Previous Design**: Single service backed by `DeduplicationStore`, enabling consistent behavior across memory and distributed backends. No separate per-pod trackers.

---

## Watermark Strategy (GitLab Poller)

**Purpose**: Track last poll time to avoid replaying historical events.

**State**: `gitlab_poller.py` → `_watermark: str | None`

**Format**: ISO 8601 timestamp (e.g., `"2025-02-19T12:34:56.789012+00:00"`)

**Lifecycle**:
1. **Initialization**: Set to `datetime.now(UTC).isoformat()` on first `start()` (avoids replaying all historical notes)
2. **Poll Cycle**:
   - Store `poll_start = datetime.now(UTC).isoformat()` before processing
   - Call `list_project_mrs(updated_after=watermark)` for each project
   - Call `list_mr_notes(created_after=watermark)` for each MR
   - After all projects processed: `watermark = poll_start`
3. **Next Cycle**: Use updated watermark as `updated_after` / `created_after` filter

**GitLab API Behavior**:
- `updated_after`: Returns MRs with `updated_at > timestamp`
- `created_after`: Returns notes with `created_at > timestamp`

**Race Condition Prevention**:
- Watermark set to poll **start** time (not end time)
- Ensures events created during poll cycle are included in next cycle
- Example:
  - Poll starts at T0, watermark=T0
  - Note created at T1 (during poll)
  - Poll ends at T2, watermark set to T0 (not T2)
  - Next poll at T3: `created_after=T0` → includes note at T1

**Edge Case**: Events created between T0 and first API call may be missed if clock skew. Mitigation: use GitLab server time from response headers (not implemented).

---

## Race Conditions Prevented

### 1. Concurrent Pushes to Same Branch

**Scenario**: Two @mention interactions on same MR, both try to push to `source_branch`.

**Without Lock**:
```
Pod A: clone → code → commit → push  ──┐
Pod B: clone → code → commit → push ───┤
                                        └→ Push conflict (non-fast-forward)
```

**With Lock** (`git_http_url` as key):
```
Pod A: acquire lock → clone → code → commit → push → release
Pod B: wait for lock ────────────────────────────────┘→ clone → ...
```

**Code**: `discussion_orchestrator.py`, `coding_orchestrator.py` both use `repo_locks.acquire()`.

---

### 2. Concurrent Branch Creation (Jira)

**Scenario**: Two Jira issues for same project, both create `agent/{issue-key}` branches.

**Without Lock**:
```
Issue A: clone → create branch "agent/proj-1" ──┐
Issue B: clone → create branch "agent/proj-2" ──┤
                                                 └→ Both succeed (different branch names)
```

**With Lock** (prevents concurrent clones):
```
Issue A: acquire lock → clone → branch → commit → push → release
Issue B: wait for lock ───────────────────────────────────────┘→ clone → ...
```

**Rationale**: Serializes access to temp clone directory, prevents directory name collisions.

---

### 3. Duplicate Webhook Delivery

**Scenario**: GitLab sends same MR webhook to multiple pods.

**Without Dedup**:
```
Pod A: receive webhook → review → post comments
Pod B: receive webhook → review → post comments  ← Duplicate comments
```

**With Dedup** (Redis):
```
Pod A: receive webhook → check dedup (miss) → mark seen → review → post
Pod B: receive webhook → check dedup (hit) → skip
```

**Code**: `gitlab_poller.py` uses `DeduplicationStore`.

**Webhook Handler**: Uses `DeduplicationService` (backed by `DeduplicationStore`) → duplicates possible in multi-pod without shared dedup backend.

---

### 4. Poller Replaying Historical Events

**Scenario**: Service restart after processing MR at T0, restarts at T1.

**Without Watermark**:
```
Poll 1: updated_after=None → returns all open MRs → processes all
Restart
Poll 2: updated_after=None → returns all open MRs again → duplicates
```

**With Watermark**:
```
Poll 1: updated_after=T0 → process new/updated MRs → watermark=T0
Restart
Poll 2: watermark=None (in-memory) → set to now() → skip historical MRs
```

**Initialization**: `_watermark` set to `now()` on first start to avoid replaying all historical notes.

---

## State Backend Selection

**Factory Functions** (`state.py`):
- `state.create_lock() -> DistributedLock`
- `state.create_dedup() -> DeduplicationStore`
- `state.create_result_store(*, azure_storage_account_url, azure_storage_connection_string, task_blob_container) -> ResultStore`
- `state.create_task_queue(*, azure_storage_queue_url, azure_storage_account_url, azure_storage_connection_string, task_queue_name, task_blob_container) -> TaskQueue`

Lock and dedup use in-memory implementations (single-controller deployment). Result store and task queue delegate to Azure Storage when configured, otherwise fall back to in-memory.

**When to Use Memory**:
- Single-pod deployments
- Development/testing
- Stateless operation acceptable

**Migration**: Switching backends requires service restart (no state migration).

---

## Performance Characteristics

| Operation | MemoryLock | RedisLock | MemoryDedup | RedisDedup |
|-----------|------------|-----------|-------------|------------|
| Acquire | O(1) | O(1) + network | O(1) | O(1) + network |
| Release | O(1) + eviction | O(1) + network | — | — |
| Is Seen | — | — | O(1) | O(1) + network |
| Mark Seen | — | — | O(1) + eviction | O(1) + network |
| Eviction | O(n) worst case | None (TTL) | O(n) worst case | None (TTL) |

**Network Latency**: Redis operations add ~1-5ms (in-cluster).

**Eviction Cost**: MemoryLock/Dedup eviction walks entire dict → O(n). Mitigate: increase `max_size`.

---

## Failure Recovery

### Redis Connection Loss

**MemoryLock**: N/A (in-process).

**RedisLock**:
- **During acquire**: Spin forever (blocks all locked operations)
- **During renewal**: Log warning, stop renewal (lock may expire)
- **During release**: Exception suppressed (lock may remain until TTL expires)

**Mitigation**: Redis replication + sentinel for HA, increase lock TTL for long operations.

---

### Service Restart

**MemoryLock**: All locks lost (no persistence).

**MemoryDedup**: All dedup state lost (potential duplicates).

**DeduplicationService**: Cleared (allows re-review and re-processing after restart with memory backend).

**RedisLock**: All locks lost (released on disconnect).

**RedisDedup**: Persisted (dedup state survives).

**Watermark**: Lost (GitLabPoller re-initializes to `now()`).

**Impact**: After restart, in-memory state lost but operations continue. Redis state persists.

---

## Monitoring

**Metrics**: None directly for locks/dedup (inferred from operation metrics).

**Logs**:
- `repo_lock_eviction`: MemoryLock evicted entries (count, max_size, current_size)
- `dedup_store_eviction`: MemoryDedup evicted entries (count, max_size, current_size)
- `processed_issue_eviction`: DeduplicationService evicted entries (issues)
- `reviewed_mr_eviction`: DeduplicationService evicted entries (reviews)
- `lock_renewal_failed`: RedisLock renewal failed (key, connection error)

**Debugging**:
- Check lock acquisition time: `reviews_duration`, `coding_tasks_duration` metrics include lock wait time
- Check dedup hit rate: compare `webhook_received_total` to `reviews_total`
