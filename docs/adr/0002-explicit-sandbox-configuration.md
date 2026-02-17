# 0002. Explicit Sandbox Configuration

## Status

**SUPERSEDED** by [ADR-0003](0003-kubernetes-migration-plan.md) — Implemented in PRs #51-#56

## Context

The `process_sandbox` module currently auto-detects whether to use `bubblewrap` (bwrap) via `shutil.which("bwrap")`, falling back to `NoopSandbox` if unavailable. While pragmatic for local development (macOS has no bwrap), this creates **operational opacity**:

- In production, we can't distinguish intentional noop mode from accidental misconfiguration
- Silent fallback masks security policy violations
- No telemetry on which sandbox is active
- devcontainer.json requires `--cap-add=SYS_ADMIN` + `seccomp=unconfined` for bwrap, which is overprivileged

**Current flow:**
```python
def get_sandbox() -> ProcessSandbox:
    if shutil.which("bwrap"):
        return BubblewrapSandbox()
    return NoopSandbox()
```

**Problem:** If bwrap is missing in production (e.g., missing from Docker image), the service silently runs unsandboxed.

## Decision

### 1. Explicit Configuration via Environment Variable

Add `SANDBOX_METHOD` to `Settings`:
- Type: `Literal["bwrap", "docker", "podman", "noop"]`
- Default: `"bwrap"`
- No `"auto"` mode — fail fast if configured method unavailable

```python
# config.py
sandbox_method: Literal["bwrap", "docker", "podman", "noop"] = Field(
    default="bwrap",
    description="Process sandbox method: bwrap (default), docker, podman, or noop (dev only)"
)
```

### 2. Refactor Factory to Accept Settings

```python
def get_sandbox(settings: Settings) -> ProcessSandbox:
    match settings.sandbox_method:
        case "bwrap":
            return BubblewrapSandbox()
        case "docker":
            return DockerSandbox()  # future
        case "podman":
            return PodmanSandbox()  # future
        case "noop":
            return NoopSandbox()
```

### 3. Add Preflight Validation to Protocol

```python
class ProcessSandbox(Protocol):
    def create_cli_wrapper(self, repo_path: str) -> str: ...
    def cleanup(self) -> None: ...
    def preflight(self) -> None:
        """Validate runtime dependencies. Raise RuntimeError if unavailable."""
        ...
```

Implementations:
- `BubblewrapSandbox.preflight()` → `shutil.which("bwrap") or raise`
- `DockerSandbox.preflight()` → `subprocess.run(["docker", "info"]) or raise`
- `NoopSandbox.preflight()` → pass (always available)

Called once at service startup (before server binds) to fail fast.

### 4. Baseline Telemetry

- **Startup:** Log `sandbox_method` at INFO level with result of preflight check
- **Session:** Emit `sandbox.select` span attribute on each `run_copilot_session`
- **Session:** Log sandbox method at DEBUG level on each CLI wrapper creation

### 5. devcontainer.json Hardening

- **Remove:** `--cap-add=SYS_ADMIN` (overprivileged)
- **Remove:** `--cap-add=SYS_ADMIN` (overprivileged)
- **Default:** Keep `seccomp=unconfined` for dev simplicity
- **Future:** Ship a minimal seccomp profile once bwrap syscall usage is audited via strace

### 6. Docker/Podman Stubs

Add `DockerSandbox` and `PodmanSandbox` classes with:
- `preflight()` checking for `docker`/`podman` binary
- `create_cli_wrapper()` raising `NotImplementedError`
- Enable config dispatch now, implementation deferred

## Alternatives Considered

### Keep Auto Mode

**Rejected:** Hides misconfiguration in production. Violates fail-fast principle.

### Default to `noop`

**Rejected:** Least secure default. Teams expecting sandboxing would silently run unsandboxed.

### Auto-detect in preflight, store result

**Rejected:** Still hides configuration. Env var makes deployment intent explicit.

## Consequences

### Positive

- **Fail Fast:** Service won't start if configured sandbox unavailable
- **Explicit Intent:** `SANDBOX_METHOD=noop` documents "I know this is unsandboxed"
- **Telemetry:** Log/trace data reveals sandbox usage patterns
- **Future-Proof:** Docker/Podman dispatch ready (implementation deferred)
- **Security:** Removes SYS_ADMIN cap from devcontainer default

### Negative

- **Breaking Change:** Existing deployments must set `SANDBOX_METHOD=bwrap` (or accept new default)
  - **Mitigation:** Default is `bwrap`, matches current behavior where available
- **One More Config Var:** Acceptable — security-critical setting should be explicit

### Backward Compatibility

- **Protocol Change:** Adding `preflight()` method to Protocol
  - **Impact:** External implementations (if any) must add `preflight()`
  - **Mitigation:** We control all implementations. No external consumers known.
- **Signature Change:** `get_sandbox()` → `get_sandbox(settings: Settings)`
  - **Impact:** All call sites must pass settings
  - **Scope:** Single call site in `copilot_session.py`

## Implementation Scope

Estimated diff: **~150 lines**

- `config.py`: +5 lines (new field + doc)
- `process_sandbox.py`: +40 lines (preflight methods, refactor factory, docker/podman stubs)
- `copilot_session.py`: +5 lines (pass settings, call preflight, log sandbox method)
- `telemetry.py`: +10 lines (span attribute)
- `test_process_sandbox.py`: +60 lines (preflight tests, dispatch tests)
- `test_config.py`: +5 lines (validate default)
- `main.py`: +10 lines (call preflight at startup)
- `devcontainer.json`: -1 line (remove cap-add)
- `.env.example` or README: +3 lines (document new var)

**Diff estimate: ~140-160 lines**

## Trade-Offs Requiring Review

### 1. Default Value: `bwrap` vs. `noop`

**Proposed:** `bwrap`
- **Pro:** Secure by default, matches current behavior when bwrap present
- **Con:** Breaks local dev on macOS unless overridden

**Alternative:** `noop`
- **Pro:** Works everywhere without config
- **Con:** Insecure by default, violates least-privilege

**Recommendation:** Keep `bwrap`. Document `SANDBOX_METHOD=noop` for local dev in README.

### 2. Fail vs. Warn on Preflight

**Proposed:** Fail (raise exception, prevent startup)
- **Pro:** Forces operator to fix config before processing webhooks
- **Con:** More disruptive

**Alternative:** Warn (log error, continue with fallback)
- **Pro:** Service stays up
- **Con:** Returns to silent misconfiguration problem

**Recommendation:** Fail. Observability without enforcement is theater.

## Isolation Progression Roadmap

| Stage | Method | Isolation | When |
|-------|--------|-----------|------|
| Current | bwrap (default) | Process namespaces + seccomp | Single host, Linux |
| Phase 1 | Docker/Podman | Container per session, resource limits | Multi-tenant, any OS |
| Phase 2 | kind/k3d | Local K8s cluster, network policies | Dev/staging scale |
| Phase 3 | Kubernetes Jobs | Full orchestration, RBAC, quotas | Production scale |

Each phase extends the `ProcessSandbox` protocol — callers never change.
The `SANDBOX_METHOD` config selects the active implementation at startup.

## Consequences
