# Deployment Guide

Docker build, Helm chart reference, k3d local dev, health checks, scaling considerations.

---

## Docker Build

### Dockerfile Overview

**Base Images**:
- `node:22-slim` → Node.js runtime (for Copilot CLI)
- `python:3.12-slim` → Python runtime

**Build Steps**:
1. Copy Node.js from node image to Python image
2. Install system dependencies: git, npm
3. Install global npm package: `@github/copilot`
4. Install uv (Python package manager)
5. Create non-root user `app:app` (UID 1000)
6. Switch to non-root user
7. Copy `pyproject.toml` + `uv.lock`, run `uv sync --no-dev --frozen`
8. Make Copilot CLI executable
9. Copy source code
10. Copy entrypoint script
11. Set CMD: `uv run uvicorn gitlab_copilot_agent.main:app --host 0.0.0.0 --port 8000`

**Security**:
- Non-root user (UID 1000)
- No setuid binaries
- Minimal attack surface (slim base images)

**Build Command**:
```bash
docker build -t gitlab-copilot-agent:latest .
```

**Multi-Platform Build** (for M1/M2 Macs):
```bash
docker buildx build --platform linux/amd64 -t gitlab-copilot-agent:latest .
```

---

## Helm Chart

### Chart Structure

```
helm/gitlab-copilot-agent/
├── Chart.yaml              # Chart metadata
├── values.yaml             # Default values
├── values-local.yaml       # Local dev overrides
└── templates/
    ├── _helpers.tpl        # Template helpers
    ├── configmap.yaml      # Non-secret config
    ├── secret.yaml         # Secrets (tokens, keys)
    ├── deployment.yaml     # Main application deployment
    ├── service.yaml        # Service (default: ClusterIP; LoadBalancer for k3d)
    ├── serviceaccount.yaml # K8s ServiceAccount
    ├── rbac.yaml           # Role + RoleBinding (Job management)
    ├── redis.yaml          # Redis StatefulSet + Service
    └── otel-collector.yaml # OTEL Collector DaemonSet
```

---

### values.yaml Reference

**Image**:
```yaml
image:
  repository: ghcr.io/peteroden/gitlab-copilot-agent
  tag: latest
  pullPolicy: IfNotPresent
```

**Controller** (main pod):
```yaml
controller:
  port: 8000
  logLevel: info
  taskExecutor: kubernetes  # or "local"
  stateBackend: redis        # or "memory"
  copilotModel: gpt-4
  copilotProviderType: ""    # "azure", "openai", or "" for Copilot
  copilotProviderBaseUrl: ""
  copilotProviderApiKey: ""
  resources:
    limits: { cpu: 500m, memory: 512Mi }
    requests: { cpu: 100m, memory: 256Mi }
```

**Redis** (optional):
```yaml
redis:
  enabled: true
  image: { repository: redis, tag: "7-alpine" }
  port: 6379
  resources:
    limits: { cpu: 250m, memory: 256Mi }
    requests: { cpu: 50m, memory: 64Mi }
  storage: 1Gi
```

**Job Runner** (K8s executor only):
```yaml
jobRunner:
  image: ""  # Defaults to controller image
  cpuLimit: "1"
  memoryLimit: 1Gi
  timeout: 600
```

**Secrets**:
```yaml
gitlab:
  url: ""                  # GITLAB_URL
  token: ""                # GITLAB_TOKEN (secret)
  webhookSecret: ""        # GITLAB_WEBHOOK_SECRET (secret)

github:
  token: ""                # GITHUB_TOKEN (secret)
```

**Service Account**:
```yaml
serviceAccount:
  create: true
  name: ""  # Auto-generated if empty
```

**Telemetry**:
```yaml
telemetry:
  otlpEndpoint: ""         # e.g., "http://otel-collector:4317"
  environment: ""          # e.g., "production"
  collector:
    image: { repository: otel/opentelemetry-collector-contrib, tag: "0.115.0" }
    resources:
      limits: { cpu: 200m, memory: 256Mi }
      requests: { cpu: 50m, memory: 128Mi }
```

**Jira** (all optional — agent runs review-only without these):
```yaml
jira:
  url: ""                  # JIRA_URL
  email: ""                # JIRA_EMAIL (secret)
  apiToken: ""             # JIRA_API_TOKEN (secret)
  projectMap: ""           # JIRA_PROJECT_MAP
  triggerStatus: "AI Ready"    # JIRA_TRIGGER_STATUS
  inProgressStatus: "In Progress"  # JIRA_IN_PROGRESS_STATUS
  inReviewStatus: "In Review"      # JIRA_IN_REVIEW_STATUS
  pollInterval: 30         # JIRA_POLL_INTERVAL
```

---

### Deployment

**Install**:
```bash
helm install copilot-agent helm/gitlab-copilot-agent \
  --set gitlab.url=https://gitlab.example.com \
  --set gitlab.token=glpat-xxxxx \
  --set gitlab.webhookSecret=my-secret \
  --set github.token=ghp_xxxxx \
  -n default --create-namespace
```

**Upgrade**:
```bash
helm upgrade copilot-agent helm/gitlab-copilot-agent \
  --set image.tag=v1.0.1 \
  -n default
```

**Uninstall**:
```bash
helm uninstall copilot-agent -n default
```

---

## k3d Local Development

### Prerequisites

- Docker Desktop
- k3d CLI
- kubectl
- Helm

### Setup

**1. Create k3d cluster**:
```bash
make k3d-up
# Creates cluster "copilot-agent-dev" with port 8080:8000 mapping
```

**2. Create .env.k3d**:
```bash
cp .env.k3d.example .env.k3d
# Edit .env.k3d with your tokens
```

**Example .env.k3d**:
```bash
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=glpat-xxxxx
GITLAB_WEBHOOK_SECRET=my-secret
GITHUB_TOKEN=ghp_xxxxx
COPILOT_PROVIDER_TYPE=
COPILOT_PROVIDER_BASE_URL=
COPILOT_PROVIDER_API_KEY=
```

**3. Build and deploy**:
```bash
make k3d-build   # Build image, import to k3d
make k3d-deploy  # Deploy via Helm
```

**4. View logs**:
```bash
make k3d-logs
# Streams logs from agent pod
```

**5. Check status**:
```bash
make k3d-status
# Shows pods, jobs, services
```

---

### Local Testing Workflow

**1. Make code changes**

**2. Rebuild and redeploy**:
```bash
make k3d-redeploy  # = k3d-build + k3d-deploy
```

**3. Test webhook**:
```bash
# Service is exposed via k3d loadbalancer — no port-forward needed
curl -s http://localhost:8080/health

# Send test webhook
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: my-secret" \
  -d @tests/fixtures/mr_webhook.json
```

**4. Check logs**:
```bash
make k3d-logs
```

**5. Inspect Jobs**:
```bash
kubectl get jobs
kubectl logs job/copilot-review-xxxxx
```

---

### Cleanup

**Delete cluster**:
```bash
make k3d-down
```

---

## Health Checks

### Endpoint: GET /health

**Returns**:
```json
{
  "status": "ok",
  "gitlab_poller": {
    "running": true,
    "failures": 0,
    "watermark": "2025-02-19T12:34:56.789012+00:00"
  }
}
```

**Fields**:
- `status`: Always `"ok"` (if service is running)
- `gitlab_poller` (optional): Present if `GITLAB_POLL=true`
  - `running`: True if poller task is active
  - `failures`: Consecutive failure count
  - `watermark`: Last poll start time (ISO 8601)

**Kubernetes Probes**:
```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 30

readinessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
```

---

## Scaling Considerations

### Single-Pod (MemoryLock, MemoryDedup)

**Limitations**:
- No horizontal scaling (state not shared)
- Single point of failure
- Limited by pod resources

**Use Cases**:
- Development
- Low-traffic deployments
- Webhook-only (no polling)

---

### Multi-Pod (RedisLock, RedisDedup)

**Requirements**:
- `STATE_BACKEND=redis`
- `REDIS_URL` configured
- Redis deployment (included in Helm chart)

**Benefits**:
- Horizontal scaling (multiple replicas)
- High availability (pod failures don't lose state)
- Webhook deduplication across pods

**Configuration**:
```yaml
controller:
  stateBackend: redis
redis:
  enabled: true
```

**Replica Count**:
```bash
helm upgrade copilot-agent helm/gitlab-copilot-agent \
  --set replicaCount=3 \
  -n default
```

**Load Balancing**: Kubernetes Service distributes webhook traffic across pods.

---

### Poller Behavior in Multi-Pod

**GitLabPoller**:
- No leader election (all pods poll independently)
- Watermark in-memory (not shared)
- Dedup via Redis prevents duplicate processing
- Recommendation: Use single replica or implement leader election

**JiraPoller**:
- Same as GitLabPoller
- ProcessedIssueTracker in-memory (each pod tracks independently)
- Recommendation: Use single replica or implement leader election

**Webhook Handler**:
- Fully stateless (scales horizontally)
- ReviewedMRTracker per-pod (duplicates possible without Redis dedup)
- Recommendation: Use Redis dedup for multi-pod

---

### Resource Requirements

**Typical Pod**:
- CPU: 100m request, 500m limit
- Memory: 256Mi request, 512Mi limit

**K8s Job Pod**:
- CPU: 1 request/limit
- Memory: 1Gi request/limit
- Adjust based on repo size and Copilot session duration

**Redis**:
- CPU: 50m request, 250m limit
- Memory: 64Mi request, 256Mi limit
- Storage: 1Gi PVC

**OTEL Collector** (if enabled):
- CPU: 50m request, 200m limit
- Memory: 128Mi request, 256Mi limit

---

### Autoscaling

**Horizontal Pod Autoscaler** (HPA):
```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: copilot-agent-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: gitlab-copilot-agent
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
```

**Apply**:
```bash
kubectl apply -f hpa.yaml -n default
```

**Monitor**:
```bash
kubectl get hpa -n default
```

---

## Secrets Management

### Kubernetes Secret

**Helm Chart**: `templates/secret.yaml`

**Fields**:
- `GITLAB_TOKEN`
- `GITLAB_WEBHOOK_SECRET`
- `GITHUB_TOKEN`
- `COPILOT_PROVIDER_API_KEY` (if BYOK)
- `JIRA_API_TOKEN` (if Jira enabled)

**Base64 Encoding**: Handled automatically by Helm.

**External Secrets Operator** (recommended for production):
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: gitlab-copilot-agent-secrets
spec:
  secretStoreRef:
    name: aws-secrets-manager
  target:
    name: gitlab-copilot-agent-secret
  data:
  - secretKey: GITLAB_TOKEN
    remoteRef:
      key: gitlab-copilot-agent/gitlab-token
  - secretKey: GITHUB_TOKEN
    remoteRef:
      key: gitlab-copilot-agent/github-token
```

---

## Troubleshooting

### Pod CrashLoopBackOff

**Check Logs**:
```bash
kubectl logs <pod-name> -n default
```

**Common Causes**:
- Missing env vars (GITLAB_TOKEN, GITHUB_TOKEN)
- Invalid REDIS_URL
- Pydantic validation error

---

### Job Pods Not Starting

**Check Job Status**:
```bash
kubectl describe job <job-name> -n default
```

**Common Causes**:
- ImagePullBackOff: Invalid `K8S_JOB_IMAGE`
- Insufficient resources: Increase cluster capacity
- RBAC issues: Check ServiceAccount has Job create permission

---

### Redis Connection Errors

**Check Redis Pod**:
```bash
kubectl get pod -l app=redis -n default
kubectl logs <redis-pod> -n default
```

**Test Connection**:
```bash
kubectl exec -it <agent-pod> -n default -- sh
# Inside pod:
redis-cli -h redis-service ping
```

---

### Webhook Not Triggering

**Check Service**:
```bash
kubectl get svc gitlab-copilot-agent -n default
```

**Verify External IP**:
- LoadBalancer: Wait for EXTERNAL-IP to be assigned
- NodePort: Use node IP + node port
- Port-Forward: `kubectl port-forward svc/gitlab-copilot-agent 8080:8000`

**GitLab Webhook Config**:
- URL: `http://<external-ip>:8000/webhook`
- Secret token: Match `GITLAB_WEBHOOK_SECRET`
- SSL verification: Disable for HTTP (or configure TLS Ingress)

**Test Manually**:
```bash
curl -X POST http://<external-ip>:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: <webhook-secret>" \
  -d '{...}'
```

---

## Production Recommendations

1. **Use Redis**: Multi-pod requires shared state
2. **Enable OTEL**: Observability critical for debugging
3. **External Secrets**: AWS Secrets Manager, HashiCorp Vault
4. **Ingress + TLS**: Use Ingress controller with cert-manager
5. **Resource Quotas**: Limit Job pod resource consumption
6. **Network Policies**: Restrict Redis access to agent pods
7. **Pod Security Standards**: Enforce restricted PSS
8. **Image Scanning**: Scan images for vulnerabilities (Trivy, Snyk)
9. **Backup Redis**: Persistent volume snapshots
10. **Monitor Metrics**: Prometheus + Grafana dashboards

---

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Deploy
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3
    
    - name: Build and push
      run: |
        docker build -t ghcr.io/${{ github.repository }}:${{ github.sha }} .
        docker push ghcr.io/${{ github.repository }}:${{ github.sha }}
    
    - name: Deploy to K8s
      run: |
        helm upgrade copilot-agent helm/gitlab-copilot-agent \
          --set image.tag=${{ github.sha }} \
          -n production
```

---

## Monitoring Checklist

- [ ] Health endpoint responding
- [ ] Pod logs show no errors
- [ ] Redis connected (if using redis backend)
- [ ] Jobs creating and completing successfully
- [ ] OTEL metrics exported (if enabled)
- [ ] Webhook endpoint accessible from GitLab
- [ ] Disk usage under threshold (Job pod TTL cleanup working)
- [ ] CPU/memory within limits
- [ ] No CrashLoopBackOff pods
- [ ] No ImagePullBackOff errors
