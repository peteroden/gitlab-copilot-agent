# 0004. Azure Container Apps Deployment Option

## Status

**ACCEPTED** — Implementation tracked by [#209](https://github.com/peteroden/gitlab-copilot-agent/issues/209)

## Context

The service runs on self-managed Kubernetes (k3d dev, Helm chart for production k8s). This works but has a high barrier to entry — teams need cluster management expertise, Helm knowledge, and ongoing k8s operations. Many teams want to deploy this agent for their own GitLab instances without managing a k8s cluster.

Azure Container Apps provides serverless container hosting with scale-to-zero, managed identity, and native Azure service integration. Combined with Terraform IaC, this creates a self-service deployment path: clone repo, fill in `.tfvars`, run `terraform apply`.

### Goals

1. **Self-service deployment**: Teams provision their own instance with Terraform
2. **Low barrier to entry**: No k8s expertise required
3. **Coexist with k8s**: Additive — existing `local` and `kubernetes` executors remain
4. **Security parity**: Match or document gaps vs k8s security controls

### Non-Goals

- Replace the k8s deployment option (that's a separate future decision)
- Production deployment (dev/staging first; production is a follow-up)
- External webhook endpoint (polling-only mode for dev)

## Decision

### Architecture: Container Apps + Jobs

Same controller/worker pattern as k8s (ADR-0003), with Azure-native equivalents:

| Component | Kubernetes | Azure Container Apps |
|-----------|-----------|---------------------|
| Controller | Deployment | Container App (scale 0→1) |
| Task execution | k8s Job | Container Apps Job (manual trigger) |
| State/results | In-cluster Redis | Azure Cache for Redis (Basic C0 dev) |
| Secrets | k8s Secret (secretKeyRef) | Azure Key Vault (secret references) |
| Networking | NetworkPolicy (pod-level) | NSG (subnet-level) |
| Identity | ServiceAccount + RBAC | User-Assigned Managed Identity |
| Observability | OTEL → collector | OTEL → Azure Monitor |
| IaC | Helm chart | Terraform modules |

### New Executor: `ContainerAppsTaskExecutor`

Third `TaskExecutor` implementation alongside `LocalTaskExecutor` and `KubernetesTaskExecutor`. Selected via `TASK_EXECUTOR=container_apps`.

Uses the Azure Container Apps Jobs API (`azure-mgmt-appcontainers`) to:
1. Start a job execution with task params as env vars
2. Poll execution status via Azure API
3. Read results from Azure Cache for Redis (same `ResultStore` protocol)

### Secret Handling (S1 — Critical)

Azure Container Apps Job executions expose env var values in Azure Activity Logs (90-day retention). To prevent secret leakage:

- **Secrets are pre-configured on the Job template** as Key Vault secret references (GITLAB_TOKEN, GITHUB_TOKEN, COPILOT_PROVIDER_API_KEY)
- **Redis auth uses Entra ID** via managed identity (REDIS_HOST + AZURE_CLIENT_ID env vars) — no Redis password in Key Vault
- **Per-execution overrides are non-sensitive only**: TASK_TYPE, TASK_ID, REPO_URL, BRANCH, SYSTEM_PROMPT, USER_PROMPT

This differs from the k8s executor where secrets can be passed per-job via `secretKeyRef`. In Container Apps, the Job template is the security boundary.

### Terraform State (S2 — Critical)

Azure Storage Account backend with:
- Encryption at rest (AES-256)
- Blob versioning enabled
- State locking via blob lease
- `.tfstate*` in `.gitignore`

### CI/CD Auth (S3 — Critical)

GitHub Actions uses OIDC workload identity federation with Azure — no long-lived service principal secrets. Federated identity scoped to `main` branch and specific resource group.

### Managed Identity Separation (S4 — High)

Two user-assigned managed identities with least-privilege RBAC:

| Identity | Permissions |
|----------|------------|
| `controller-identity` | ACR pull, Key Vault read (all secrets), Container Apps Job trigger, Log Analytics contributor |
| `job-identity` | Key Vault read (task secrets only), Redis data access |

### Accepted Risk: Lost Container Security Controls (S5 — High)

Container Apps Jobs do **not** support:
- `readOnlyRootFilesystem`
- Linux capability drops (`drop: [ALL]`)
- Pod-level NetworkPolicy

**Compensating controls:**
- Short job TTL (execution timeout, automatic cleanup)
- Patch validation (path traversal check, `MAX_PATCH_SIZE` limit)
- Human MR review gate (agent output is advisory, not auto-merged)
- NSG rules at subnet level (restrict egress to GitLab/GitHub/Redis)
- Managed identity (no long-lived credentials in containers)

### Accepted Risk: Redis Basic Tier (S6 — High, dev only)

Azure Cache for Redis Basic tier lacks encryption at rest. Acceptable for dev:
- Short TTL on cached data (1 hour)
- Non-production data only
- **Production must use Standard or Premium tier** (follow-up issue)

## Alternatives Considered

### Azure Kubernetes Service (AKS)

Managed k8s control plane but still requires cluster management, node pool sizing, and k8s expertise. Defeats the low-barrier-to-entry goal. Rejected.

### AWS Fargate / Google Cloud Run

Comparable serverless offerings but introduces multi-cloud complexity. Team standardized on Azure. Rejected.

### Delay Until Container Apps Adds securityContext

Unknown timeline. Business value of self-service deployment outweighs the security gap, which has compensating controls. Rejected.

## Consequences

### Positive

- **Self-service**: Teams deploy with `terraform apply` — no k8s expertise needed
- **Serverless**: Scale to zero, pay per execution, no cluster management
- **Additive**: Three executors coexist (`local`, `kubernetes`, `container_apps`)
- **Clean abstraction**: `TaskExecutor` protocol accommodates all three without business logic changes

### Negative

- **Security regression**: Lost `readOnlyRootFilesystem` and capability drops (mitigated by compensating controls)
- **Azure lock-in**: Key Vault references, managed identity are non-portable (Terraform abstracts some risk)
- **Platform maturity**: Container Apps GA since 2022 — less battle-tested than k8s
- **Cost**: Azure Cache for Redis + Container Apps Environment has baseline cost even at zero scale

## References

- [ADR-0003: Kubernetes Migration Plan](0003-kubernetes-migration-plan.md) — establishes TaskExecutor protocol
- [Azure Security Review](ADR-AZURE-CONTAINER-APPS-SECURITY.md) — detailed security analysis
- [Azure Security Risk Acceptance](AZURE-RISK-ACCEPTANCE-SECURITYCONTEXT.md) — securityContext gap analysis
- [Issue #209](https://github.com/peteroden/gitlab-copilot-agent/issues/209) — implementation tracking
