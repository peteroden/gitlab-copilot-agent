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
- `MemoryLock` (`concurrency.py`): In-process asyncio locks with LRU eviction
- `RedisLock` (`redis_state.py`): Distributed lock using Redis SET NX + TTL

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
1. **MR `/copilot` command** (`mr_comment_handler.py`):
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
- `MemoryDedup` (`concurrency.py`): In-process set with size-based eviction
- `RedisDedup` (`redis_state.py`): Redis SET key with TTL

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
- Webhook: None (uses `ReviewedMRTracker` in-memory)
- Poller: 86400s (24 hours)

**Purpose**: Prevent duplicate reviews on same commit.

**Tracked By**:
- **Webhook**: `ReviewedMRTracker` (in-memory, per-pod)
  - Tuple: `(project_id, mr_iid, head_sha)`
  - Marked after successful review in `_process_review()`
- **Poller**: `DeduplicationStore` (memory or Redis)
  - Marked in `gitlab_poller.py` → `_process_mr()`

**Race Condition**: 
- **Webhook**: Multiple pods receive same webhook → each pod checks own `ReviewedMRTracker` → duplicate reviews possible
- **Poller**: Single poller instance → no race (watermark ensures MRs processed once per cycle)

**Mitigation**: Use Redis dedup store for webhook in multi-pod setup.

---

### 2. /copilot Notes (Poller Only)

**Key**: `note:{project_id}:{mr_iid}:{note_id}`

**TTL**: 86400s (24 hours)

**Purpose**: Prevent re-processing same `/copilot` comment.

**Tracked By**: `DeduplicationStore` in `gitlab_poller.py` → `_process_notes()`

**No Webhook Dedup**: Webhook handler does not dedupe (assumes GitLab sends each note event once).

---

### 3. Jira Issues (Poller Only)

**Key**: `issue.key` (e.g., `"PROJ-123"`)

**TTL**: Session lifetime (in-memory `ProcessedIssueTracker`)

**Purpose**: Prevent re-processing same issue in a single poller run.

**Tracked By**: `ProcessedIssueTracker` in `jira_poller.py`

**Persistence**: Cleared on service restart (allows re-processing after restart).

---

## ReviewedMRTracker

**Purpose**: In-memory tracker for reviewed (project_id, mr_iid, head_sha) tuples.

**Data Structure**: `OrderedDict[tuple[int, int, str], None]`

**Behavior**:
1. `is_reviewed(project_id, mr_iid, head_sha)`: Check if tuple in dict
2. `mark(project_id, mr_iid, head_sha)`: Add tuple, evict if needed
3. Eviction: Same as `MemoryDedup` (oldest 50% when exceeds `max_size`)

**Usage**: `webhook.py` → checks before dispatching `_process_review()`, marks after success.

**Why Separate from DeduplicationStore?**
- Per-pod state (each pod tracks its own processed MRs)
- Cleared on restart (allows re-review after pod restart)
- Fast lookup (no Redis roundtrip)

**Multi-Pod Caveat**: In multi-pod setup, different pods don't share state → duplicate reviews possible if same webhook delivered to multiple pods.

---

## ProcessedIssueTracker

**Purpose**: In-memory tracker for processed Jira issue keys.

**Data Structure**: `OrderedDict[str, None]`

**Behavior**: Same as `MemoryDedup`.

**Usage**: `jira_poller.py` → checks before dispatching `CodingOrchestrator.handle()`, marks after success.

**Persistence**: Cleared on restart (allows re-processing issues after restart).

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

**Scenario**: Two `/copilot` commands on same MR, both try to push to `source_branch`.

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

**Code**: `mr_comment_handler.py`, `coding_orchestrator.py` both use `repo_locks.acquire()`.

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

**Webhook Handler**: Uses `ReviewedMRTracker` (per-pod, not shared) → duplicates possible in multi-pod without Redis.

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

**Config**: `STATE_BACKEND` env var (`"memory"` or `"redis"`)

**Factory Functions**:
- `redis_state.create_lock(backend, redis_url, *, redis_host, redis_port, azure_client_id) -> DistributedLock`
- `redis_state.create_dedup(backend, redis_url, *, redis_host, redis_port, azure_client_id) -> DeduplicationStore`

When `redis_host` is provided, the factories use Microsoft Entra ID authentication (via `redis-entraid` + `DefaultAzureCredential`) instead of the connection string URL. This eliminates password management for Azure deployments.

**When to Use Redis**:
- Multi-pod deployments (horizontal scaling)
- K8s executor (Job results stored in Redis)
- Long-running service (persist dedup across restarts)

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

**ReviewedMRTracker**: Cleared (allows re-review).

**ProcessedIssueTracker**: Cleared (allows re-processing Jira issues).

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
- `processed_issue_eviction`: ProcessedIssueTracker evicted entries
- `reviewed_mr_eviction`: ReviewedMRTracker evicted entries
- `lock_renewal_failed`: RedisLock renewal failed (key, connection error)

**Debugging**:
- Check lock acquisition time: `reviews_duration`, `coding_tasks_duration` metrics include lock wait time
- Check dedup hit rate: compare `webhook_received_total` to `reviews_total`
