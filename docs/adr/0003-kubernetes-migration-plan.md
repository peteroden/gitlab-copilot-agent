# 0003. Kubernetes Migration Plan

## Status

**ACCEPTED** — All decisions resolved, implementation tracked by [#77](https://github.com/peteroden/gitlab-copilot-agent/issues/77)

## Context

The GitLab Copilot Agent runs as a single-process FastAPI service with in-memory state (dedup trackers, per-repo locks) and process-level sandboxing (bwrap/Docker/Podman). This architecture cannot scale horizontally — all state is lost on restart, tasks compete for CPU/memory within one process, and a single long-running Copilot session can degrade the webhook handler.

ADR-0002 established the `ProcessSandbox` protocol and anticipated a progression from bwrap → containers → k8s Jobs. This ADR defines the concrete plan for that migration and **supersedes ADR-0002's sandbox progression model** — the intermediate steps (bwrap, Docker/Podman containers) are removed entirely in favor of k8s-native pod isolation.

### Goals

1. **Dev/prod parity**: Same k8s manifests run locally (k3d) and in production
2. **Horizontal scaling**: Multiple pods handling tasks concurrently
3. **Execution isolation**: Each review/coding task runs in its own k8s Job pod
4. **Same orchestration code everywhere**: No "local mode" vs "k8s mode" branching in business logic

### Non-Goals

- Microservice decomposition (premature for 1–2 dev team)
- Custom operators or CRDs
- Multi-cluster federation

## Decision

### Architecture: Controller + Worker Jobs

```
┌─────────────────────────────────────────────────────┐
│                   k8s Cluster                        │
│                                                      │
│  ┌──────────────────────────────────┐                │
│  │     Controller (Deployment)      │                │
│  │                                  │                │
│  │  ┌────────────┐ ┌────────────┐  │                │
│  │  │  Webhook    │ │  Jira      │  │                │
│  │  │  Listener   │ │  Poller    │  │                │
│  │  └─────┬───────┘ └─────┬──────┘  │                │
│  │        │               │          │                │
│  │        └───────┬───────┘          │                │
│  │                │                  │                │
│  │         Job Dispatcher            │                │
│  │          (k8s API)                │                │
│  └───────────────┬──────────────────┘                │
│                  │                                    │
│        ┌─────────┼─────────┐                         │
│        ▼         ▼         ▼                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐             │
│  │ Job Pod  │ │ Job Pod  │ │ Job Pod  │             │
│  │ (Review) │ │ (Coding) │ │ (Review) │             │
│  │          │ │          │ │          │             │
│  │ Clone →  │ │ Clone →  │ │ Clone →  │             │
│  │ Copilot  │ │ Copilot  │ │ Copilot  │             │
│  │ Session  │ │ Session  │ │ Session  │             │
│  └──────────┘ └──────────┘ └──────────┘             │
│                                                      │
│  ┌──────────┐                                        │
│  │  Redis   │  ← Locks, dedup, task status           │
│  │  (Pod)   │                                        │
│  └──────────┘                                        │
└─────────────────────────────────────────────────────┘
```

**Why this pattern:**

- Minimal surgery from current monolith — ingress (webhook + Jira poller) stays in one Deployment, only execution moves to k8s Jobs
- New `TaskExecutor` protocol encapsulates the dispatch→wait→collect lifecycle (see below)
- Naturally evolves: if webhook API needs independent scaling later, extract it (no rewrite)
- 1 Deployment + N Jobs is operationally simple for a small team

### Abstraction Boundary: TaskExecutor Protocol

The existing `ProcessSandbox` protocol returns a local CLI path for `CopilotClientOptions.cli_path`. This works for in-process execution (bwrap, Docker, noop) but **cannot** accommodate k8s Job dispatch — the Copilot session runs inside the remote Job pod, not locally.

The abstraction must be lifted from CLI wrapping to task execution:

```python
@dataclass
class TaskParams:
    task_type: Literal["review", "coding"]
    task_id: str
    repo_url: str
    branch: str
    system_prompt: str
    user_prompt: str
    settings: Settings

class TaskExecutor(Protocol):
    """Execute a Copilot session and return the result."""
    async def execute(self, task: TaskParams) -> str: ...
```

Two implementations:

- **`LocalTaskExecutor`** — runs `run_copilot_session()` directly in-process (no sandbox). Used for local dev, docker-compose, and tests where the process or container boundary provides sufficient isolation.
- **`KubernetesTaskExecutor`** — creates a k8s Job with task params as env vars, watches Job completion, reads results from Redis, returns them to the caller. The k8s pod boundary provides isolation.

Selected via config: `TASK_EXECUTOR=local|k8s`.

**Why not extend ProcessSandbox:** ADR-0002's `ProcessSandbox` protocol is designed for CLI wrapping (returns a local path). This interface cannot accommodate k8s Job dispatch where the Copilot session runs remotely. Rather than force-fitting the protocol, `TaskExecutor` replaces it entirely. See "Sandbox Simplification" below.

### Local Kubernetes: k3d

**Why k3d over kind/k0s:**

| Factor | k3d | kind | k0s |
|--------|-----|------|-----|
| Startup time | ~20s | ~60s | ~30s |
| Built-in registry | Yes (k3d registry) | No (manual setup) | No |
| Image loading | `k3d image import` | `kind load docker-image` | Manual |
| Resource overhead | Low (k3s under hood) | Medium (full kubelet) | Low |
| Docker-native | Yes | Yes | Not Docker-native |
| CI friendly | Yes | Yes | Less common |
| k8s conformance | Full (k3s is certified) | Full | Full |

k3d is the best fit: fast startup, built-in registry for local image builds, lightweight, and fully conformant. **Switching to kind later is trivial** — only the cluster lifecycle wrapper changes (~50 lines); all manifests, Helm charts, and application code are identical.

### State Management: Custom Python Abstractions over Redis

Three primitives need externalizing:

| Primitive | Current | k8s Implementation |
|-----------|---------|-------------------|
| Per-repo lock | `asyncio.Lock` in `RepoLockManager` | Redis distributed lock (Redlock) |
| Issue dedup | `OrderedDict` in `ProcessedIssueTracker` | Redis SET with TTL |
| MR review dedup | `OrderedDict` in `ReviewedMRTracker` | Redis SET with TTL |

**Design: Protocol + pluggable backends**

```python
class DistributedLock(Protocol):
    @asynccontextmanager
    async def acquire(self, key: str, ttl_seconds: int = 300) -> AsyncIterator[None]: ...

class DeduplicationStore(Protocol):
    async def is_seen(self, key: str) -> bool: ...
    async def mark_seen(self, key: str, ttl_seconds: int = 3600) -> None: ...
```

**Key serialization:** The current `ReviewedMRTracker` uses `tuple[int, int, str]` keys (project_id, mr_iid, head_sha). The `DeduplicationStore` protocol uses `str` keys for backend portability. Callers serialize composite keys to a canonical string format (e.g., `f"{project_id}:{mr_iid}:{head_sha}"`) at the call site. No backward-compatibility wrapper — this is internal code.

Two implementations:
- `MemoryLock` / `MemoryDedup` — current behavior, for tests and single-pod mode
- `RedisLock` / `RedisDedup` — for k8s mode

**Redis lock implementation:** `RedisLock` will use the `redis-py` library's built-in `Lock` class (single-instance Redlock), not a custom implementation. This is a well-tested, maintained lock with proper TTL, retry, and extension support. Custom Redlock implementations are error-prone (see Kleppmann's analysis) and unnecessary when a library exists.

Selected via config: `STATE_BACKEND=memory|redis` + `REDIS_URL`.

**Why not Dapr:** Dapr adds an operator, sidecar per pod, CRDs, and a learning curve — disproportionate for 3 primitives on a 1–2 dev team. The Protocol-based design gives equal portability. Dapr can be added later as another implementation without any interface changes.

### Sandbox Simplification: Remove bwrap/DinD/Podman

With k8s providing pod-level isolation, the intermediate sandbox methods from ADR-0002 are no longer needed and are **removed entirely**:

| Removed | Reason |
|---------|--------|
| `BubblewrapSandbox` | Requires `SYS_ADMIN` / `seccomp=unconfined` — a security liability we've been trying to harden away from (issue #55). k8s pod isolation is strictly stronger. |
| `ContainerSandbox` (Docker/Podman) | Requires Docker socket access or `--privileged` for DinD — larger attack surface than k8s pods. Podman removal already proposed in #65. |
| `ProcessSandbox` protocol | Replaced by `TaskExecutor` protocol. No remaining implementations need it. |
| `Dockerfile.sandbox` | Sandbox-specific image no longer built. Single `Dockerfile` for the agent. |
| `scripts/build-sandbox-image.sh` | No sandbox image to build. |
| `SANDBOX_METHOD` config | Replaced by `TASK_EXECUTOR=local\|k8s`. |

This subsumes issues #55 (seccomp profile for bwrap) and #65 (drop podman) — both become unnecessary.

**What remains:**
- `LocalTaskExecutor` — runs Copilot CLI directly in-process. Used for local dev, docker-compose, and tests. The host process or Docker container boundary provides isolation.
- `KubernetesTaskExecutor` — dispatches to k8s Job pods with full pod-level isolation.

**Pod isolation in k8s** replaces all previous sandbox methods. The Job pod:
- Runs the Copilot CLI directly (no nested sandbox)
- Has resource limits (CPU, memory, ephemeral storage)
- Has network policy (egress to GitLab API, GitHub API, and in-cluster Redis)
- Is ephemeral — destroyed after task completion
- Has `ttlSecondsAfterFinished` for automatic cleanup
- Has baseline `securityContext` from Phase 1 (see below)

**Phase 1 securityContext (required — not deferred):** The removed `ContainerSandbox` enforced `--cap-drop=ALL`, `--read-only`, `--security-opt=no-new-privileges`, and resource limits. Phase 1 Job pods must match or exceed this security posture:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities:
    drop: [ALL]
```

With `emptyDir` mounts for writable paths (`/tmp`, `/home`, clone directory). Phase 3 adds the full restricted pod security standard and `PodDisruptionBudget`.

### Packaging: Helm

Helm chart with values files for environment-specific config:
- `values-local.yaml` — k3d settings (lower resources, `k3d image import` workflow)
- `values-prod.yaml` — production settings (higher resources, GHCR image pull, managed Redis)

Why Helm over Kustomize: We need dynamic Job specs (image tags, env vars, resource limits vary per task type). Helm's Go templates handle this naturally; Kustomize's patch model makes it fragile.

### Job Communication Pattern

The controller and Job pods communicate via:

1. **Dispatch**: Controller creates k8s Job with task parameters as env vars / ConfigMap
2. **Status**: Controller watches Job status via k8s API (Running → Succeeded/Failed)
3. **Results**: Job writes results to Redis (keyed by job ID) or directly posts to GitLab/Jira
4. **Cleanup**: `ttlSecondsAfterFinished: 3600` auto-deletes completed Jobs

**Option A (simpler):** Job pod is self-contained — it clones the repo, runs Copilot, and posts results directly to GitLab/Jira. Controller only needs to watch for completion/failure.

**Option B (controller-mediated):** Job pod writes results to Redis. Controller reads results and posts to GitLab/Jira.

**Decision:** Option B (controller-mediated). Job pods write results (review JSON, coding summary) to Redis keyed by job ID. The controller watches Job completion, reads results from Redis, and posts to GitLab/Jira. This centralizes external API interaction (GitLab, Jira) in the controller, simplifying retry logic and result formatting.

**Credential reality:** Job pods still need `GITHUB_TOKEN` (Copilot SDK auth) and `GITLAB_TOKEN` (git clone). The credential surface is similar to the controller — the benefit of Option B is not fewer credentials, but that Job pods don't need to understand GitLab's review comment API or Jira's transition API.

**Result durability risk:** If Redis crashes between a Job writing its result and the controller reading it, the task's work is lost (LLM tokens consumed, no result delivered). Unlike dedup state loss (which just causes a harmless re-review), result loss is unrecoverable work. Mitigations:
- Job pods also store a summary in a k8s Job annotation (`results.copilot-agent/summary`) as a fallback. Controller checks annotations if Redis read fails.
- Redis persistence (`appendonly yes`) enabled in the Helm chart to survive Redis pod restarts.
- Phase 3: Redis Sentinel for HA.

### Redis Hosting

In-cluster Redis (Helm subchart) for Phase 1. The `REDIS_URL` config makes switching to managed Redis (ElastiCache, Memorystore, etc.) a config change — no code modifications needed.

### Image Registry: GHCR

Agent images published to GitHub Container Registry (`ghcr.io`). Simplest integration with the existing GitHub-hosted repo. k3d uses `k3d image import` for local dev (no registry push needed).

### CI Pipeline (No CD)

GitHub Actions CI workflow:
- Build + test on every PR
- Build + push Docker image to GHCR on merge to main
- No automated deployment — manual `helm upgrade` for now
- CD pipeline will be added closer to production readiness

## Implementation Phases

### Phase 1: Foundation — Externalize State + k8s Job Sandbox

**Goal:** Run in k8s with proper isolation. Single controller replica.

| Work Item | Description | Est. Diff |
|-----------|-------------|----------|
| Sandbox removal | Delete `BubblewrapSandbox`, `ContainerSandbox`, `ProcessSandbox` protocol, `Dockerfile.sandbox`, `build-sandbox-image.sh`, `SANDBOX_METHOD` config. Update tests. Closes #55, #65. | ~-400 lines (net deletion) |
| TaskExecutor protocol | `TaskExecutor` protocol + `LocalTaskExecutor` (wraps `run_copilot_session` directly) + update callers | ~120 lines |
| State protocols | `DistributedLock` and `DeduplicationStore` protocols + memory implementations (refactor existing classes) | ~100 lines |
| Redis implementations | `RedisLock` (using `redis-py` built-in Lock) and `RedisDedup` (SET + TTL) | ~120 lines |
| Config additions | `STATE_BACKEND`, `REDIS_URL`, `TASK_EXECUTOR` settings | ~30 lines |
| `KubernetesTaskExecutor` | Creates k8s Job, watches status, reads results from Redis (+ annotation fallback) | ~200 lines |
| Task runner entrypoint | Alternate entrypoint for Job pods: parse task params → clone → run Copilot session → write results to Redis + Job annotation | ~150 lines |
| Helm chart | Controller Deployment, Job template (with securityContext), Redis (with AOF persistence), Service, Secrets, ConfigMap, RBAC, imagePullSecrets | ~300 lines (may split across 2 PRs) |
| k3d dev setup | `Makefile` targets: `make k3d-up`, `make k3d-deploy`, `make k3d-down` | ~80 lines |
| Tests | Unit tests for new protocols + Redis impls + TaskExecutors | ~200 lines |

**Total estimated: ~1,300 lines across 7–8 PRs**

### Phase 2: Scaling + Leader Election

**Goal:** Handle higher webhook throughput. Multiple controller replicas.

- Split webhook handler into its own Deployment
- Add k8s Lease-based leader election for Jira poller (only leader polls)
- Webhook Deployment scales to N replicas
- **Trigger:** When webhook latency becomes a bottleneck

### Phase 3: Observability + Hardening

**Goal:** Production-grade operations.

- Full pod security standards (restricted profile — Phase 1 provides baseline `securityContext`)
- Network policies (Job pods → GitLab + GitHub APIs + in-cluster Redis only)
- Job-level metrics and alerting (completion rate, duration, failure rate)
- Log aggregation from ephemeral Job pods
- Resource quota per namespace
- `PodDisruptionBudget` for controller
- Redis Sentinel for HA

### Phase 4: Event-Driven (if needed)

**Goal:** Decouple ingress from dispatch for burst traffic.

- Redis Streams or NATS between controller and Job dispatch
- Enables backpressure, replay, dead-letter handling
- **Trigger:** When burst traffic overwhelms direct Job creation

## Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Job startup latency | 5–30s added to task start (pod scheduling + image pull) | Pre-pull images on nodes; acceptable for 1–10 min tasks |
| Job pod failures | Tasks fail silently if pod crashes | Controller watches Job status; `backoffLimit: 1` for infrastructure failures (OOMKilled, image pull, node eviction). Idempotency guard: task runner checks Redis for existing result before starting. |
| Redis SPOF | Distributed state + Job results in Redis | Phase 1: Redis AOF persistence + Job annotation fallback for results. Dedup loss is harmless (re-review). Phase 3: Redis Sentinel. |
| Redis result loss | Job completes but result lost before controller reads | Job annotation fallback (`results.copilot-agent/summary`). Controller checks annotation if Redis read fails. |
| Credential management | Job pods need GITLAB_TOKEN, GITHUB_TOKEN | k8s Secrets mounted as env vars — standard pattern |
| GITHUB_TOKEN lifetime | Tokens injected at Job creation; if pod queues before scheduling, effective lifetime shrinks | GitHub App tokens expire in 1h. For long-queued Jobs, controller should verify token freshness or use short-lived token refresh. PATs are not affected. |
| Image pull secrets | GHCR may be private; Job pods need pull access | Helm chart includes `imagePullSecrets` configuration. k3d uses `k3d image import` (no pull needed). |
| Job cleanup | Completed/failed pods accumulate | `ttlSecondsAfterFinished: 3600` + monitoring |
| k3d ↔ prod drift | Local behavior differs from production | Identical Helm chart; only `values-*.yaml` differs (resource limits, image tags) |
| Image build in CI | Need to build + push agent image before deploy | CI pushes to GHCR on merge; k3d uses `k3d image import` for local |

## Alternatives Considered

### Microservices + Message Bus (Pattern B/C)

**Rejected for Phase 1.** Splitting into 3 Deployments + a message bus (Redis pub/sub or NATS) optimizes for independent scaling before there's evidence it's needed. A 1–2 person team should not operate 3 services on day one. The Controller + Jobs pattern naturally evolves into this if needed.

### Dapr for State Management

**Rejected.** Dapr adds an operator, sidecar injector, placement service, and component CRDs — disproportionate for 3 primitives. The Protocol-based abstraction provides equal portability. Can be adopted later as another backend implementation.

### Docker-in-Docker Sidecar in Jobs

**Rejected.** DinD requires privileged pods, adds container startup latency, and increases attack surface. k8s pod isolation is sufficient — the pod boundary provides namespace, cgroup, and network isolation equivalent to the current container sandbox.

### Keeping bwrap/DinD Alongside k8s

**Rejected.** Maintaining 4 sandbox methods (bwrap, Docker, Podman, k8s) for 2 actual deployment targets (local dev, k8s) is unnecessary complexity. bwrap requires `SYS_ADMIN` capabilities, DinD requires `--privileged` — both are security liabilities that k8s pod isolation makes obsolete. The `LocalTaskExecutor` (noop, in-process) covers local dev and tests. Removing the intermediate methods deletes ~400 lines of code, eliminates `Dockerfile.sandbox`, simplifies the test matrix from 4 to 2, and closes issues #55 and #65.

### Monolith with Leader Election (Pattern D)

**Rejected.** Doesn't solve the core problem — tasks still run in-process, competing for resources and risking crash propagation. Leader election without execution isolation is a lateral move.

### Kustomize for Packaging

**Rejected.** Kustomize's patch-based model is fragile for dynamic Job specs. Helm's templating handles environment-specific config (image tags, resource limits, env vars) more naturally.

## Consequences

### Positive

- **Isolation:** Each task in its own pod — no resource contention, no crash propagation
- **Scaling:** k8s handles scheduling, resource allocation, and pod lifecycle
- **Dev/prod parity:** Same Helm chart locally (k3d) and in production
- **Clean abstraction:** `TaskExecutor` protocol replaces `ProcessSandbox` entirely. Two implementations (`LocalTaskExecutor`, `KubernetesTaskExecutor`) cover all deployment targets.
- **Simplification:** Removing bwrap/DinD/Podman deletes ~400 lines of code, `Dockerfile.sandbox`, and the `SANDBOX_METHOD` config. Test matrix drops from 4 sandbox methods to 2 task executors. Closes issues #55 and #65.
- **Incremental:** Phase 1 is self-contained; Phases 2–4 are optional and independently valuable

### Negative

- **Operational complexity:** k8s is more complex than a single Docker container
  - Mitigated by k3d for local dev and Helm for reproducible deployments
- **Job startup latency:** 5–30s overhead per task
  - Acceptable for 1–10 min tasks
- **Redis dependency:** New infrastructure component
  - Phase 1 can run with `STATE_BACKEND=memory` for testing
- **Learning curve:** k8s concepts (Jobs, RBAC, Secrets, NetworkPolicy)
  - Mitigated by Helm chart abstracting most of this

## PR Sequence (Phase 1)

| PR | Issue | Scope | Agent | Depends On |
|----|-------|-------|-------|------------|
| 0 | #TBD | Sandbox removal: delete bwrap/DinD/Podman code, `Dockerfile.sandbox`, `SANDBOX_METHOD`. Closes #55, #65. | @developer | — |
| 1 | #78 | TaskExecutor protocol + `LocalTaskExecutor` + update callers | @architect → @developer | PR 0 |
| 2 | #79 | State protocols (`DistributedLock`, `DeduplicationStore`) + memory implementations | @architect → @developer | — |
| 3 | #80 | Redis implementations + `STATE_BACKEND` config | @developer | #79 |
| 4 | #81 | Task runner entrypoint for Job pods | @architect → @developer | #78 |
| 5 | #82 | `KubernetesTaskExecutor` implementation | @architect → @developer | #78, #79, #81 |
| 6 | #83 | Helm chart (Controller, Job template w/ securityContext, Redis w/ AOF, RBAC, imagePullSecrets) | @architect → @developer | #81, #82 |
| 7 | #84 | k3d dev setup (`Makefile` targets, dev workflow) | @developer | #83 |
| 8 | #TBD | Integration tests (k3d cluster, e2e Job dispatch) | @architect → @developer | #84 |

PR 0 (sandbox removal) lands first — it's a net deletion that unblocks clean `TaskExecutor` implementation. PRs 1 (#78) and 2 (#79) can then be developed in parallel. PR 4 (#81) can start once PR 1 lands. Helm chart (#83) may split into 2 PRs if it exceeds 200 lines.

Tracked by epic: [#77](https://github.com/peteroden/gitlab-copilot-agent/issues/77)
