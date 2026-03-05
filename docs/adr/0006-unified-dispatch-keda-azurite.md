# 0006. Unified Dispatch via KEDA + Azurite — Eliminate Redis Entirely

## Status

**ACCEPTED** — 2026-03-05

## Context

ADR-0005 replaced Redis-based ACA dispatch with Azure Storage Queue + Blob Storage
(Claim Check pattern). Phase 2 E2E testing confirmed the pattern works: webhook,
GitLab poller, and Jira poller all successfully dispatch through Azure Storage Queue
to ACA Jobs and receive results via Blob Storage.

However, the K8s executor still uses a completely separate dispatch mechanism:
- **Dispatch**: env vars injected into Job pod spec
- **Results**: Redis `SET`/`GET` by task_id
- **Job creation**: direct K8s API call from controller

This means:
1. **Two code paths** for the same logical operation (dispatch + collect result)
2. **Redis still required** for any K8s deployment
3. **K8s executor cannot use KEDA** (jobs are created imperatively, not event-driven)
4. **No local development path** without Redis or Azure subscription

### Proposed Change

Use **KEDA ScaledJob + Azurite** to give the K8s executor the same Azure Storage
Queue dispatch pattern as ACA, eliminating Redis and unifying the codebase to a
single dispatch mechanism regardless of hosting platform.

**Azurite** is the official Azure Storage emulator. It speaks the identical REST API
as Azure Storage (Queue, Blob, Table) and runs as a single lightweight container
(~50MB). The Python SDK's `from_connection_string()` works identically against
Azurite and real Azure — the only difference is the endpoint URL.

**KEDA** (Kubernetes Event-Driven Autoscaling) watches the Azure Storage Queue and
creates K8s Jobs automatically when messages appear. KEDA's `azure-queue` scaler
supports Azurite via `cloud: Private` + `endpointSuffix` configuration.

## Decision

### Unified Architecture

```
┌─────────── CONTROLLER (same code, any platform) ──────────────┐
│                                                                │
│  1. Upload params blob ───► Storage (Azure or Azurite)         │
│  2. Enqueue message ──────► Storage Queue (Azure or Azurite)   │
│  3. Poll result blob ◄────── Storage Blob (Azure or Azurite)   │
│                                                                │
└────────────────────────────────────────────────────────────────┘

      ┌──────────────── KEDA ScaledJob (both platforms) ─────────┐
      │  Watches queue → creates Job/Execution per message       │
      │  ACA: KEDA scale rule (built-in)                         │
      │  K8s: KEDA ScaledJob CRD (installed via Helm)            │
      └──────────────────────────────────────────────────────────┘

┌─────────── TASK RUNNER (same code, any platform) ─────────────┐
│  4. Dequeue message ◄─── Storage Queue                         │
│  5. Read params blob ◄── Storage Blob                          │
│  6. Execute task                                               │
│  7. Upload result blob ──► Storage Blob                        │
│  8. Delete queue message ─► Storage Queue                      │
└────────────────────────────────────────────────────────────────┘
```

### Authentication Strategy

| Platform | Auth Mechanism | Config Field |
|----------|---------------|--------------|
| ACA (Azure) | `DefaultAzureCredential` (managed identity) | `azure_storage_account_url` + `azure_storage_queue_url` |
| K8s (Azurite) | Connection string (well-known dev key) | `azure_storage_connection_string` |
| K8s (real Azure) | Connection string or workload identity | `azure_storage_connection_string` |

The factory in `azure_storage.py` branches on whether `connection_string` is provided:
- **Yes**: `QueueClient.from_connection_string()` / `ContainerClient.from_connection_string()`
- **No**: `DefaultAzureCredential()` with URL-based construction (current behavior)

### KEDA Configuration

**K8s (Azurite)**:
```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledJob
metadata:
  name: task-runner
spec:
  jobTargetRef:
    parallelism: 1
    completions: 1
    template:
      spec:
        containers:
        - name: task
          image: task-runner:latest
          command: [".venv/bin/python", "-m", "gitlab_copilot_agent.task_runner"]
          envFrom:
          - secretRef:
              name: task-runner-secrets
        restartPolicy: Never
  pollingInterval: 10
  maxReplicaCount: 10
  successfulJobsHistoryLimit: 5
  failedJobsHistoryLimit: 3
  triggers:
  - type: azure-queue
    metadata:
      queueName: task-queue
      queueLength: "1"           # One job per message
      accountName: devstoreaccount1
      cloud: Private
      endpointSuffix: "azurite.default.svc:10001"
    authenticationRef:
      name: azurite-trigger-auth
```

**ACA (Azure)**: KEDA scale rule on the Container App Job resource (replaces
`begin_start()` ARM API call):
```hcl
resource "azurerm_container_app_job" "task_runner" {
  # ... existing config ...

  event_trigger_config {
    parallelism              = 1
    replica_completion_count = 1

    scale {
      min_executions = 0
      max_executions = 10
      polling_interval_in_seconds = 10

      rules {
        name = "queue-trigger"
        type = "azure-queue"
        metadata = {
          queueName  = "task-queue"
          queueLength = "1"
          accountName = azurerm_storage_account.tasks.name
        }
        authentication {
          secret_name       = "storage-connection"
          trigger_parameter = "connection"
        }
      }
    }
  }
}
```

### Azurite Deployment (K8s)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: azurite
spec:
  replicas: 1
  selector:
    matchLabels:
      app: azurite
  template:
    spec:
      containers:
      - name: azurite
        image: mcr.microsoft.com/azure-storage/azurite:latest
        ports:
        - containerPort: 10000  # Blob
        - containerPort: 10001  # Queue
        - containerPort: 10002  # Table
        volumeMounts:
        - name: data
          mountPath: /data
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: azurite-data
---
apiVersion: v1
kind: Service
metadata:
  name: azurite
spec:
  selector:
    app: azurite
  ports:
  - name: blob
    port: 10000
  - name: queue
    port: 10001
  - name: table
    port: 10002
```

### What Gets Deleted

| Component | Lines | Reason |
|-----------|-------|--------|
| `k8s_executor.py` env var dispatch | ~100 | Replaced by queue dispatch |
| `k8s_executor.py` K8s Job creation | ~80 | KEDA creates jobs |
| `task_runner.py` env var fallback path | ~30 | Queue is the only path |
| `redis_state.py` Redis result store | ~60 | BlobResultStore for all |
| `redis_state.py` Redis lock manager | ~40 | Queue provides serialization |
| `config.py` Redis fields | ~20 | No longer needed |
| `pyproject.toml` redis deps | ~2 | `redis`, `redis-entraid` removed |
| `infra/redis.tf` | ~50 | Entire file deleted |

**Estimated net deletion: ~350+ lines of Redis/env-var code.**

## Research Findings — Gotchas and Mitigations

### Confirmed Working ✅

| Concern | Finding |
|---------|---------|
| KEDA + Azurite | Works with `cloud: Private` + `endpointSuffix` pointing at Azurite K8s Service |
| Python SDK + Azurite | `from_connection_string()` works identically; Azurite V3 supports latest API versions |
| KEDA ScaledJob 1:1 | `queueLength: "1"` + `parallelism: 1` + `completions: 1` = one job per message |
| Azurite Queue visibility timeout | Supported (our 5-min timeout works) |
| Azurite Blob read/write | Full CRUD support, sufficient for Claim Check pattern |

### Known Limitations ⚠️

| Limitation | Impact | Mitigation |
|------------|--------|------------|
| **Azurite has no blob lifecycle policies** | Result/param blobs won't auto-expire | Add a CronJob or controller-side cleanup sweep (delete blobs older than 24h) |
| **Azurite uses shared-key auth only** | No managed identity, no Azure AD | Use connection string auth; secret stored in K8s Secret. Acceptable for in-cluster emulator |
| **Azurite is single-instance** | No HA, no geo-replication | Acceptable for task dispatch (controller retries on failure; queue messages have visibility timeout). Use PVC for persistence across pod restarts |
| **KEDA polling interval** | Min 10s delay between queue check and job creation | Acceptable — current `begin_start()` adds similar latency from ARM API calls |
| **Azurite not for production data** | No SLA, no backup | Only used for transient task dispatch data (params + results). Real review content is posted to GitLab/Jira, not stored in Azurite |
| **ACA KEDA event trigger requires connection string** | Can't use managed identity for the KEDA trigger itself (only for the app container) | Store connection string as ACA secret; the app container still uses managed identity for data operations |
| **KEDA `azure-queue` scaler counts ALL messages** | Includes invisible (in-flight) messages in queue length | With `queueLength: "1"`, KEDA may over-provision jobs that find no messages to dequeue. Task runner already handles empty dequeue gracefully (exits cleanly) |

### Non-Issues ✅

| Concern | Why It's Fine |
|---------|---------------|
| Azurite Docker image size | ~50MB, negligible vs task runner image |
| Connection string in K8s Secret | Standard practice; same as current Redis URL secret |
| KEDA installation | Single Helm chart (`keda/keda`), widely adopted, CNCF graduated |
| Table Storage on Azurite | Supported for dedup (Phase 3); basic CRUD works fine |

## Alternatives Considered

### Keep Redis for K8s, Azure Storage for ACA
- **Pro**: No new K8s infrastructure (Azurite, KEDA)
- **Con**: Two dispatch code paths forever. Redis operational burden. Cannot unify executor logic.
- **Rejected**: The whole point is eliminating Redis and code duplication.

### Use RabbitMQ/NATS instead of Azurite for K8s
- **Pro**: Purpose-built message brokers with richer features
- **Con**: Different API than Azure Storage Queue — need a second TaskQueue implementation. More operational complexity.
- **Rejected**: Azurite gives us API-identical behavior with zero code changes.

### Embed queue in SQLite/filesystem for local K8s
- **Pro**: Zero external dependencies
- **Con**: Custom queue implementation, no KEDA integration, poor concurrency semantics.
- **Rejected**: Reinventing what Azurite already provides.

## Consequences

### Positive
- **One dispatch pattern** for all platforms (ACA, K8s, local dev)
- **Redis fully eliminated** — no Redis infrastructure, no Redis dependencies, no Redis auth complexity
- **KEDA handles job lifecycle** — no imperative Job creation code, automatic scale-to-zero
- **~350+ lines deleted**, ~50 lines added (connection string factory + Azurite manifests)
- **Local development** can use Azurite in devcontainer (no Azure subscription needed)
- **Testability** — integration tests can run against Azurite in CI

### Negative
- **New K8s dependencies**: KEDA operator + Azurite deployment (both well-established)
- **Azurite is a SPOF** in K8s deployments (mitigated by PVC persistence + pod restart)
- **Connection string management** for KEDA trigger auth (standard K8s Secret pattern)
- **Blob cleanup** needs explicit implementation (no lifecycle policy in Azurite)

## Implementation Phases

### Phase 3A: Connection String Auth Support (~80 lines, 1 PR)
- Add `azure_storage_connection_string` to `Settings`
- Branch factory in `azure_storage.py`: connection string vs DefaultAzureCredential
- Update `create_task_queue()` and `create_blob_result_store()` callers
- Tests with mock connection string path

### Phase 3B: KEDA Event Trigger for ACA (~45 lines net negative, 1 PR)
- Switch ACA Job from `manual_trigger_config` to `event_trigger_config` (KEDA queue trigger)
- Remove `_start_execution()`, `_get_execution_status()`, `_create_client()` from `aca_executor.py`
- Remove `azure-mgmt-containerservice` / `azure-mgmt-appcontainers` dependency
- Controller just enqueues + polls result blob (no ARM API calls)
- E2E test on `rg-copilot-storage-test`

### Phase 3C: K8s Azurite + KEDA Manifests (~100 lines, 1 PR)
- Azurite Deployment + Service + PVC
- KEDA ScaledJob manifest for task-runner
- TriggerAuthentication with Azurite connection string
- Helm values for Azurite endpoint configuration
- Init container or Job to create queue + blob container in Azurite on first deploy

### Phase 3D: Unify K8s Executor (~150 lines net negative, 1 PR)
- K8s executor uses TaskQueue (same as ACA) instead of env var dispatch
- Remove K8s Job creation code (KEDA handles it)
- Remove env var fallback from task_runner.py
- Remove Redis result store from K8s path
- Tests

### Phase 3E: Remove Redis (~200 lines deleted, 1 PR)
- Delete Redis-specific code from `redis_state.py`
- Remove `redis`, `redis-entraid` from dependencies
- Remove Redis config from `Settings`
- Delete `infra/redis.tf` (already conditional)
- Update documentation

## References

- [KEDA Azure Storage Queue scaler](https://keda.sh/docs/2.19/scalers/azure-storage-queue/)
- [KEDA ScaledJob specification](https://keda.sh/docs/2.19/reference/scaledjob-spec/)
- [Azurite emulator documentation](https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azurite)
- [Sample KEDA Queue Jobs](https://github.com/tomconte/sample-keda-queue-jobs)
- [Azurite Helm chart](https://github.com/viters/azurite-helm-chart)
- ADR-0005: Azure Storage Queue Dispatch (Claim Check pattern)
