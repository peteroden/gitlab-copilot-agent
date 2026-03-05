# 0005. Replace Redis Dispatch with Azure Storage Queue + Blob Storage (Claim Check)

## Status

**ACCEPTED** — Protocol design finalized 2026-03-04

## Context

The ACA executor (`aca_executor.py`) dispatches task params via Redis keyed by
`CONTAINER_APP_JOB_EXECUTION_NAME` and triggers job executions with `begin_start()`.
The original RPOP concurrency bug (#238) was fixed by PR #240 (keyed dispatch), but
the architecture still relies on Redis for dispatch, results, dedup, and execution
sentinels — plus ARM API calls to start jobs.

The k8s executor passes params as env vars per-Job, which avoids these issues but
couples dispatch to the k8s API.

### Goals

1. Fix the ACA concurrency bug by design (1 message = 1 execution, guaranteed)
2. Eliminate Redis as the dispatch and result-storage mechanism
3. Enable event-driven ACA Job scaling (KEDA watches queue, no ARM `begin_start()` calls)
4. Keep the `TaskExecutor` protocol stable — no changes to callers
5. Preserve the option for k8s executor to use env vars (no forced Azure dependency)

### Non-Goals

- Change the TaskExecutor protocol's public signature
- Rewrite the k8s executor (it works; Azure Storage is opt-in for k8s)
- Feature flags or gradual rollout (single deployment, cut over directly)

## Decision

### Pattern: Claim Check with Azure Storage Queue + Blob Storage

```
┌────────────────────────── CONTROLLER ──────────────────────────┐
│                                                                │
│  1. Upload params blob ─────────► Azure Blob Storage           │
│     params/{task_id}.json          (private endpoint)          │
│                                                                │
│  2. Enqueue message ────────────► Azure Storage Queue          │
│     {"task_id": "..."}             (private endpoint)          │
│                                                                │
│  3. Poll for result blob ◄──────── Azure Blob Storage          │
│     results/{task_id}.json         (same container)            │
│                                                                │
└────────────────────────────────────────────────────────────────┘

      ┌─────────────────── KEDA (automatic) ──────────────────┐
      │  Watches queue length → triggers ACA Job execution    │
      └───────────────────────────────────────────────────────┘

┌────────────────────────── TASK RUNNER ─────────────────────────┐
│  (ACA Job execution — one per queue message)                   │
│                                                                │
│  4. Dequeue message ◄───────────── Azure Storage Queue         │
│     (visibility timeout = 5 min)                               │
│                                                                │
│  5. Read params blob ◄──────────── Azure Blob Storage          │
│     params/{task_id}.json                                      │
│                                                                │
│  6. Execute task (clone, copilot session, build result)         │
│                                                                │
│  7. Upload result blob ─────────► Azure Blob Storage           │
│     results/{task_id}.json                                     │
│                                                                │
│  8. Delete queue message ───────► Azure Storage Queue           │
│     (acknowledges completion)                                  │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### Why This Architecture Is Better

The original RPOP concurrency bug (#238) was fixed by keyed dispatch (PR #240), but
the current architecture still requires Redis + ARM API calls + execution sentinels.

With event-driven KEDA triggers:
- Each queue message triggers exactly one job execution
- The triggered execution dequeues its own message (visibility timeout prevents others)
- If the execution fails, the message reappears after the visibility timeout
- No `begin_start()` ARM API call — KEDA handles triggering

The correlation is structural, not logical. One message in → one execution out.

---

## Detailed Design

### 1. KEDA Event-Driven Trigger (Question 1)

**KEDA does not pass the message to the container.** It monitors queue length and triggers
job executions. The job container reads its own message.

Flow:
1. KEDA scaler polls queue via `queueLength` metric
2. When messages ≥ 1, KEDA triggers an ACA Job execution
3. Job container starts, calls `get_messages(num_messages=1, visibility_timeout=300)`
4. Message becomes invisible to other executions for 5 minutes
5. Job processes task, writes result, then deletes message
6. If job crashes before delete: visibility timeout expires, message reappears, KEDA
   triggers a new execution (automatic retry)

**Scaling**: `queueLength=1` means 1 message = 1 execution. ACA Job max parallelism
controls the upper bound.

**Poison queue**: Azure Storage Queue tracks `dequeue_count`. After 5 dequeues (configurable),
move to `{queue-name}-poison`. Task runner checks dequeue count and writes an error result
blob instead of retrying indefinitely.

### 2. K8s Executor Strategy (Question 2)

**K8s keeps env vars as the default dispatch mechanism.** The concurrency bug doesn't
exist in k8s because each Job gets its own env vars — dispatch and execution are 1:1
by construction.

K8s gets a new **opt-in** result backend: Blob Storage instead of Redis. This is useful
for Azure-hosted k8s (AKS) where the team wants to minimize Redis usage.

```
                          ┌─ env vars (default) ──── works today, no change
K8s executor ─── dispatch ┤
                          └─ Storage Queue (opt-in) ─ future, for AKS consistency

                          ┌─ Redis (current default)
K8s executor ─── results  ┤
                          └─ Blob Storage (opt-in) ── new BlobResultStore
```

Config: `DISPATCH_BACKEND=env_vars|azure_storage` (k8s only; ACA always uses azure_storage).

### 3. Result Readiness Detection (Question 3)

**Poll blob existence.** The controller already polls in a loop (`_wait_for_result`).
We replace "poll Redis key" with "poll blob existence":

```python
async def _wait_for_result(self, task_id: str, task_type: str) -> TaskResult:
    deadline = asyncio.get_event_loop().time() + self._settings.aca_job_timeout
    while asyncio.get_event_loop().time() < deadline:
        blob = await self._result_store.get(task_id)
        if blob is not None:
            return _parse_result(blob, task_type)
        await asyncio.sleep(_POLL_INTERVAL)
    raise TimeoutError(f"Task {task_id} timed out after {self._settings.aca_job_timeout}s")
```

**Why not a result queue or Event Grid webhook?**
- Result queue: adds a second queue to manage, controller must correlate messages
- Event Grid: adds infrastructure, latency is fine with 5s polling
- Blob poll: simplest, matches existing pattern, no new infra beyond what's needed

**Error signaling**: The result blob itself carries success/failure:

```json
{"status": "success", "result_type": "coding", "summary": "...", "patch": "...", "base_sha": "..."}
{"status": "failed", "error": "Agent did not return valid output", "task_id": "..."}
```

The controller no longer needs to poll ACA execution status via ARM API. Blob existence
_is_ the completion signal. Timeout is the failure backstop.

### 4. Locking and Deduplication (Question 4)

**Dedup and execution sentinels collapse into the queue.**

With Storage Queue + KEDA:
- **Execution dedup**: Queue visibility timeout ensures one consumer per message. No sentinel needed.
- **Repo lock**: Unnecessary for remote executors (isolated containers + unique branches).
- **Polling dedup**: Pollers send messages; idempotent jobs tolerate occasional duplicates after restarts.

Redis remains for `DistributedLock` until a future phase replaces it with Blob leases (#245).
Dedup moves to Table Storage in Phase 3 — same Storage Account, zero extra cost. The Jira
poller and coding orchestrator adopt the existing `DeduplicationStore` protocol, replacing
their in-memory `set[str]` tracking.

| Concern | Current | Proposed | Rationale |
|---------|---------|----------|-----------|
| Task dispatch | Redis keyed SET | Azure Storage Queue | Queue semantics, KEDA trigger |
| Task results | Redis KV | Azure Blob Storage | Handles 10MB patches |
| Execution sentinel | Redis SET NX | Queue visibility timeout | Built into queue |
| Repo lock | Redis lock | Removed | Unnecessary for isolated containers |
| Dedup | Redis SET + TTL | Table Storage | Same Storage Account, zero extra cost |
| Dedup (Jira) | In-memory set | Table Storage via `DeduplicationStore` | Unifies with GitLab dedup |

### 5. Protocol and Interface Changes (Question 5)

**`TaskExecutor` protocol: NO CHANGE.** Callers continue to call `execute(task) -> TaskResult`.

**New `TaskQueue` protocol.** Backend-agnostic dispatch abstraction. Claim Check is
transparent — callers pass full JSON payloads; the implementation decides whether to
inline or externalize to blob. The protocol is designed for substitutability — swapping
backends requires only a new implementation class, no caller changes.

**`ResultStore` protocol: SIMPLIFIED.** Queue methods removed; use `TaskQueue` instead.

**`DeduplicationStore` protocol: UNCHANGED.** Adopted by Jira poller and coding
orchestrator (replacing in-memory `set[str]`). New `TableStorageDedup` implementation
replaces `RedisDedup` for ACA.

#### New File: `src/gitlab_copilot_agent/dispatch.py`

```python
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class QueueMessage:
    """Handle for a dequeued message.

    Created by TaskQueue.dequeue(). Passed back to TaskQueue.complete().
    Callers read task_id, payload, and dequeue_count.
    message_id and receipt are opaque — used only by complete().
    """

    message_id: str       # opaque: Azure message ID, SB sequence, SQS message ID
    receipt: str          # opaque: Azure pop_receipt, SB lock_token, SQS receipt_handle
    task_id: str          # extracted from message body
    payload: str          # full params JSON (loaded via Claim Check transparently)
    dequeue_count: int    # how many times this message has been dequeued


@runtime_checkable
class TaskQueue(Protocol):
    """Enqueue tasks for async workers and dequeue on the worker side.

    Implementations handle the Claim Check pattern transparently: large
    payloads are stored externally (e.g., Blob Storage) and retrieved
    on dequeue. Callers never interact with the external store directly.
    """

    async def enqueue(self, task_id: str, payload: str) -> None:
        """Submit a task for processing.

        The implementation decides whether to inline the payload in the
        queue message or store it externally (Claim Check).
        """
        ...

    async def dequeue(self, visibility_timeout: int = 300) -> QueueMessage | None:
        """Retrieve the next available message, or None if empty.

        The message becomes invisible to other consumers for
        visibility_timeout seconds. If complete() is not called within
        that window, the message reappears and dequeue_count increments.
        """
        ...

    async def complete(self, message: QueueMessage) -> None:
        """Acknowledge successful processing. Deletes the queue message.

        Does NOT delete the external payload blob (if any). Blob cleanup
        is handled by lifecycle policy. Idempotent.
        """
        ...

    async def aclose(self) -> None:
        """Release underlying connections and clients."""
        ...
```

#### Simplified ResultStore (queue methods removed)

```python
@runtime_checkable
class ResultStore(Protocol):
    """Read/write task results by key."""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int = 3600) -> None: ...
    async def aclose(self) -> None: ...
```

#### New Implementations

| Class | Protocol | Backend | File |
|-------|----------|---------|------|
| `AzureStorageTaskQueue` | `TaskQueue` | Storage Queue + Blob (Claim Check) | `azure_storage.py` |
| `BlobResultStore` | `ResultStore` | Azure Blob Storage | `azure_storage.py` |
| `TableStorageDedup` | `DeduplicationStore` | Azure Table Storage | `azure_storage.py` |
| `MemoryTaskQueue` | `TaskQueue` | In-memory (for tests) | `dispatch.py` |
| `MemoryResultStore` / `BlobResultStore` | `ResultStore` | In-memory or Azure Blob Storage | `state.py` / `azure_storage.py` |
| `MemoryResultStore` | `ResultStore` | In-memory (simplified) | `concurrency.py` |

Swapping backends requires only a new class implementing the same protocol.

#### Claim Check: Queue Message vs. Blob Split

Azure Storage Queue messages are limited to 64 KB. System prompts + user prompts can
approach this limit. The Claim Check pattern splits the payload:

- **Queue message** (small): `{"task_id": "abc-123"}` — trigger only
- **Params blob** (unbounded): `params/{task_id}.json` — full `TaskParams` serialization
- **Result blob** (unbounded): `results/{task_id}.json` — full `TaskResult` serialization

The `AzureStorageTaskQueue.enqueue()` method:
1. Uploads params blob to `params/{task_id}.json`
2. Enqueues `{"task_id": "..."}` to the Storage Queue

The `AzureStorageTaskQueue.dequeue()` method:
1. Gets one message from the queue (with visibility timeout)
2. Returns `QueueMessage` with `task_id` extracted from message body
3. Caller reads params blob separately via `ResultStore.get()` or direct blob read

#### Executor Constructor Changes

```python
# Before
class ContainerAppsTaskExecutor:
    def __init__(self, settings: Settings, result_store: ResultStore) -> None: ...

# After
class ContainerAppsTaskExecutor:
    def __init__(self, settings: Settings, result_store: ResultStore, task_queue: TaskQueue) -> None: ...
```

The k8s executor constructor does not change (env var dispatch doesn't need a queue).
If k8s opts into Storage Queue later, the same pattern applies.

#### Task Runner Changes

```python
# Before: _load_dispatch_params() reads from Redis list
# After:  _load_dispatch_params() reads from Azure Storage Queue

async def _load_dispatch_params() -> tuple[dict[str, str], QueueMessage] | None:
    """Dequeue task from Azure Storage Queue + read params blob (Claim Check)."""
    queue = create_task_queue(...)
    msg = await queue.dequeue(visibility_timeout=300)
    if msg is None:
        return None
    blob_store = create_result_store(...)
    params_json = await blob_store.get(f"params/{msg.task_id}")
    return json.loads(params_json), msg


async def _store_result(task_id: str, result: str) -> None:
    """Write result to Azure Blob Storage."""
    store = create_result_store(...)
    await store.set(f"results/{task_id}", result)
```

### 6. Azure Resources and Terraform Changes (Question 6)

#### New Azure Resources

| Resource | Purpose | Terraform Resource |
|----------|---------|-------------------|
| Storage Account | Host queue + blobs + table | `azurerm_storage_account` |
| Storage Queue | Task dispatch messages | `azurerm_storage_queue` |
| Blob Container | Params + result blobs | `azurerm_storage_container` |
| Storage Table | Dedup entries (TTL-on-read) | `azurerm_storage_table` |
| Private Endpoint | Keep traffic on VNet | `azurerm_private_endpoint` |
| Private DNS Zone | Blob + Queue + Table DNS | `azurerm_private_dns_zone` (×3) |
| DNS VNet Link | Link DNS to VNet | `azurerm_private_dns_zone_virtual_network_link` (×3) |
| Subnet | PE subnet for Storage | `azurerm_subnet` |
| NSG Rule | Allow storage traffic | security_rule in existing NSG |
| RBAC: Controller | Queue send + Blob write/read + Table write | `azurerm_role_assignment` (×3) |
| RBAC: Job | Queue receive + Blob write/read + Table read | `azurerm_role_assignment` (×3) |
| Lifecycle Policy | Auto-delete old blobs | `azurerm_storage_management_policy` |

#### New File: `infra/storage.tf`

```hcl
resource "azurerm_storage_account" "tasks" {
  name                     = "st${replace(var.resource_group_name, "-", "")}tasks"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"          # Dev; use ZRS for prod
  min_tls_version          = "TLS1_2"

  # Disable shared key — Entra ID only (matches Redis auth pattern)
  shared_access_key_enabled = false

  # Private only
  public_network_access_enabled = false

  blob_properties {
    delete_retention_policy { days = 7 }
  }

  tags = var.tags
}

resource "azurerm_storage_queue" "tasks" {
  name                 = "task-queue"
  storage_account_name = azurerm_storage_account.tasks.name
}

resource "azurerm_storage_container" "tasks" {
  name                  = "task-data"
  storage_account_name  = azurerm_storage_account.tasks.name
  container_access_type = "private"
}

resource "azurerm_storage_table" "dedup" {
  name                 = "dedup"
  storage_account_name = azurerm_storage_account.tasks.name
}

# Auto-delete blobs older than 24 hours (replaces Redis TTL)
resource "azurerm_storage_management_policy" "task_cleanup" {
  storage_account_id = azurerm_storage_account.tasks.id
  rule {
    name    = "cleanup-task-data"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["task-data/"]
    }
    actions {
      base_blob {
        delete_after_days_since_creation_greater_than = 1
      }
    }
  }
}
```

#### Networking: New Subnet + Private Endpoints

```hcl
# variables-apps.tf
variable "storage_subnet_prefix" {
  description = "CIDR for the Storage Account private endpoint subnet"
  type        = string
  default     = "10.0.4.0/24"
}

# networking.tf — new subnet
resource "azurerm_subnet" "storage" {
  name                 = "snet-storage"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.storage_subnet_prefix]
}

# NSG rule — allow Container Apps → Storage PE
security_rule {
  name                       = "AllowStorageOutbound"
  priority                   = 130
  direction                  = "Outbound"
  access                     = "Allow"
  protocol                   = "Tcp"
  source_port_range          = "*"
  destination_port_range     = "443"
  source_address_prefix      = var.infra_subnet_prefix
  destination_address_prefix = var.storage_subnet_prefix
}

# Private endpoints for blob and queue (separate sub-resources)
resource "azurerm_private_endpoint" "storage_blob" {
  name                = "pe-stblob-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.storage.id
  private_service_connection {
    name                           = "storage-blob-connection"
    private_connection_resource_id = azurerm_storage_account.tasks.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "blob-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.storage_blob.id]
  }
}

resource "azurerm_private_endpoint" "storage_queue" {
  name                = "pe-stqueue-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.storage.id
  private_service_connection {
    name                           = "storage-queue-connection"
    private_connection_resource_id = azurerm_storage_account.tasks.id
    subresource_names              = ["queue"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "queue-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.storage_queue.id]
  }
}
```

#### RBAC Assignments

```hcl
# Controller: send messages + read/write blobs
resource "azurerm_role_assignment" "controller_queue_sender" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Queue Data Message Sender"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

resource "azurerm_role_assignment" "controller_blob" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

# Job: receive/delete messages + read/write blobs
resource "azurerm_role_assignment" "job_queue_processor" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Queue Data Message Processor"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

resource "azurerm_role_assignment" "job_blob" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}
```

#### ACA Job: Manual → Event-Driven Trigger

```hcl
# Before
resource "azurerm_container_app_job" "task_runner" {
  ...
  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }
  ...
}

# After
resource "azurerm_container_app_job" "task_runner" {
  ...
  event_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
    scale {
      min_executions = 0
      max_executions = 5   # Max concurrent task executions
      rules {
        name = "task-queue"
        type = "azure-queue"
        metadata = {
          queueName    = azurerm_storage_queue.tasks.name
          queueLength  = "1"                                  # 1 msg = 1 execution
          accountName  = azurerm_storage_account.tasks.name
        }
        authentication {
          secret_name       = "storage-connection"
          trigger_parameter = "connection"
        }
      }
    }
  }
  ...

  # New env vars for task runner
  template {
    container {
      ...
      env {
        name  = "AZURE_STORAGE_ACCOUNT_URL"
        value = azurerm_storage_account.tasks.primary_blob_endpoint
      }
      env {
        name  = "AZURE_STORAGE_QUEUE_URL"
        value = azurerm_storage_account.tasks.primary_queue_endpoint
      }
      env {
        name  = "TASK_QUEUE_NAME"
        value = azurerm_storage_queue.tasks.name
      }
      env {
        name  = "TASK_BLOB_CONTAINER"
        value = azurerm_storage_container.tasks.name
      }
      ...
    }
  }
}
```

**Note**: KEDA Azure Queue scaler currently requires a connection string for auth
(stored as an ACA secret). Managed identity support for KEDA scalers in ACA is
in preview. Start with connection string for the KEDA scaler only; all application
code uses managed identity via `DefaultAzureCredential`.

#### Controller: Remove ARM API Permission (Eventually)

The controller currently needs `Contributor` on the ACA Job to call `begin_start()`.
After migration, this role assignment can be removed — the controller only interacts
with Storage Queue + Blob, never with the ACA Jobs API.

```hcl
# DELETE after migration is complete:
# resource "azurerm_role_assignment" "controller_job_start" { ... }
```

### 7. Migration Strategy (Question 7)

**No feature flag.** Single deployment (demo), cut over directly.

#### Phase 1: Infrastructure (Terraform only, no code changes)

Deploy Storage Account, Queue, Blob Container, Table, Private Endpoints, DNS, RBAC.
The ACA Job stays on `manual_trigger_config`. No code changes.

**Rollback**: `terraform destroy` the new resources.

#### Phase 2: Application Code + Cutover

1. Add `TaskQueue` protocol + `QueueMessage` in new `dispatch.py`
2. Add `AzureStorageTaskQueue` + `BlobResultStore` in new `azure_storage.py`
3. Add `MemoryTaskQueue` for tests
4. Update `aca_executor.py`: enqueue via `TaskQueue`, poll `ResultStore` for results
   - Delete `_start_execution()`, `_create_client()`, `_get_execution_status()`
   - Delete execution sentinel logic
5. Update `task_runner.py`: dequeue via `TaskQueue`, store via `ResultStore`, complete message
6. Update `main.py`: wire `TaskQueue` + `BlobResultStore` into ACA executor
7. Switch ACA Job from `manual_trigger_config` to `event_trigger_config` (KEDA)
8. Remove `push_task`, `pop_task`, `remove_task` from `ResultStore` protocol

Deploy in this order:
1. Terraform: Storage Account + Queue + Blob + Table + RBAC
2. Code: new protocols + implementations + wiring
3. Terraform: switch trigger config (manual → event-driven)

**Rollback**: Revert code + Terraform to `manual_trigger_config` + keyed Redis dispatch.

#### Phase 3: Dedup Migration + ACA Redis Cleanup

1. Add `TableStorageDedup` implementing `DeduplicationStore` in `azure_storage.py`
2. Jira poller + coding orchestrator adopt `DeduplicationStore` (replace in-memory `set[str]`)
3. Delete `ProcessedIssueTracker` from `concurrency.py`
4. Wire `TableStorageDedup` as the dedup backend for ACA
5. Remove Redis dispatch/result/dedup code from ACA path
6. Remove `Contributor` role on ACA Job from controller identity
7. Remove `azure.mgmt.appcontainers` dependency from ACA executor

After Phase 3, ACA has zero Redis dependency. Redis remains only for k8s (#245).

**Rollback**: Git revert + redeploy.

#### K8s + Full Redis Elimination

Tracked separately in GitHub issue #245.

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| KEDA scaler doesn't trigger reliably | Low | High | Integration test with real ACA Job. KEDA Azure Queue scaler is well-established. Monitor `keda_scaler_errors` metric. |
| Visibility timeout too short (job still processing when message reappears) | Medium | Medium | Set visibility timeout to match `job_timeout` (600s). Task runner extends timeout periodically if processing takes longer. |
| Blob Storage latency higher than Redis for polling | Low | Low | Blob read is ~10ms vs Redis ~1ms. Polling interval is 5s. Negligible impact. |
| KEDA connection string secret rotation | Medium | Medium | Use short-lived SAS tokens or monitor for managed identity GA in KEDA ACA. Document rotation procedure. |
| Poison queue messages accumulate | Low | Low | Lifecycle policy auto-deletes. Monitor poison queue depth. Alert on depth > 0. |
| Storage Account outage | Very Low | High | Azure Storage has 99.9% SLA (LRS). Same risk profile as Redis. Use ZRS in prod for 99.99%. |
| Migration window — both backends active, inconsistent state | Medium | Medium | Feature flag ensures clean cutover. No split-brain: one backend active at a time per executor. |
| Params blob contains prompts (data sensitivity) | N/A | N/A | Already handled: private endpoint, encryption at rest (Azure default), Entra ID auth, no shared keys. Same trust boundary as current Redis. |

### Security Considerations

- **No new trust boundaries**: Storage Account is on the same VNet, behind private endpoint,
  same as Redis and Key Vault. Controller and Job identities already exist.
- **No secrets in queue messages**: Queue message contains only `task_id`. Params are in
  blob (encrypted at rest). Secrets (tokens, API keys) remain in Key Vault.
- **Shared key disabled**: `shared_access_key_enabled = false` on Storage Account.
  All access via Entra ID + managed identity.
- **KEDA scaler exception**: KEDA requires a connection string (stored as ACA Job secret).
  This is the only use of a connection string. Scope it to queue-only SAS if possible.

### Observability

| Signal | Metric/Log | Source |
|--------|-----------|--------|
| Task dispatched | `task.dispatched` counter, `task_id` tag | Controller structured log |
| Queue depth | `azure.storage.queue.message_count` | Azure Monitor metric |
| Message dequeued | `task.dequeued` counter, `task_id`, `dequeue_count` | Task runner structured log |
| Result blob written | `task.result_stored` counter, `task_id`, `blob_size` | Task runner structured log |
| Result blob read | `task.result_retrieved` counter, `task_id`, `poll_count` | Controller structured log |
| Poison message | `task.poison` counter, `task_id` | Task runner structured log + alert |
| Polling timeout | `task.timeout` counter, `task_id` | Controller structured log + alert |
| Blob/Queue errors | `task.storage_error` counter, `operation`, `error_code` | Both, structured log |

---

## Alternatives Considered

### Azure Service Bus instead of Storage Queue

**Evaluated in depth.** Service Bus Standard ($10/month) provides built-in MessageId
dedup (24h window), dead-letter queue, PeekLock, and 256KB messages (no Claim Check
needed). This eliminates ~100 lines of dedup/poison/Claim Check code compared to
Storage Queue. However, the $10/month base cost replaces $0.01/month Storage Queue
for ~100 lines of straightforward code. Decision: not justified for a demo service.
The `TaskQueue` protocol is designed so a `ServiceBusTaskQueue` implementation can be
swapped in later if dedup complexity grows.

### Redis Streams instead of Storage Queue

Fixes the RPOP race with consumer groups. But: keeps Redis dependency for dispatch,
no KEDA event-driven ACA trigger, doesn't address the "minimize Redis" goal. Rejected.

### Event Grid + Blob trigger instead of Storage Queue

Controller writes blob, Event Grid triggers ACA Job. But: Event Grid adds latency
(~seconds), more complex Terraform, harder to test locally. Storage Queue + KEDA is
the standard ACA pattern. Rejected.

### Async fire-and-forget with callback

Change `TaskExecutor.execute()` to return immediately, with a callback when done.
But: every caller (orchestrator, coding workflow, review engine) would need rewriting.
Massive blast radius for no benefit — the controller already handles concurrency via
the orchestrator's repo lock. Rejected.

### Move locking/dedup to Table Storage now

Could eliminate Redis entirely in one migration. But: high risk, two migrations at once,
Table Storage TTL requires manual cleanup or Azure Functions timer. Separate concern,
separate ADR. Rejected for now.

## Consequences

### Positive

- **Concurrency bug fixed by design**: 1 message → 1 execution, enforced by queue semantics
- **No ARM API calls**: Controller never calls `begin_start()`. Simpler, fewer permissions.
- **Built-in retry**: Visibility timeout + poison queue = automatic retry without custom code
- **Cost reduction**: Storage Queue/Blob is ~$0.01/month vs. Redis Basic at ~$16/month
  (once Redis is fully eliminated in Phase 5)
- **Cleaner protocol**: `ResultStore` loses unrelated queue methods
- **Observable**: Queue depth and blob metrics are built into Azure Monitor

### Negative

- **KEDA connection string**: One credential that isn't Entra ID (until KEDA MI support GAs)
- **Polling latency**: Blob poll adds ~10ms per check vs Redis ~1ms (negligible at 5s interval)
- **More infrastructure**: Storage Account + 2 Private Endpoints + DNS zones + subnet
- **Migration complexity**: 5-phase rollout with feature flag management
- **Azure lock-in deepens**: Storage Queue is Azure-specific (mitigated: `TaskQueue` protocol
  abstracts the backend; swap implementation for SQS/GCP without caller changes)

## References

- [ADR-0004: Azure Container Apps Deployment](0004-azure-container-apps-migration.md) — establishes ACA architecture
- [Claim Check Pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/claim-check) — Microsoft architecture pattern
- [ACA Jobs Event-Driven Triggers](https://learn.microsoft.com/en-us/azure/container-apps/tutorial-event-driven-jobs) — KEDA + ACA Jobs
- [KEDA Azure Storage Queue Scaler](https://keda.sh/docs/latest/scalers/azure-storage-queue/) — KEDA scaler docs
