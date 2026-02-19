# Configuration Reference

Every environment variable in `config.py`, grouped by category.

---

## Core Settings

### `GITLAB_URL`
- **Type**: `str`
- **Required**: ✅ Yes
- **Description**: GitLab instance URL (e.g., `https://gitlab.example.com`)
- **Validation**: Must be valid URL

### `GITLAB_TOKEN`
- **Type**: `str`
- **Required**: ✅ Yes
- **Description**: GitLab API private token with `api` scope
- **Security**: Read/write access to all allowed projects, webhook processing
- **Validation**: Non-empty string

### `GITLAB_WEBHOOK_SECRET`
- **Type**: `str`
- **Required**: ✅ Yes
- **Description**: Secret for validating webhook payloads via HMAC (X-Gitlab-Token header)
- **Security**: Must match GitLab webhook configuration
- **Validation**: Non-empty string

---

## Authentication (LLM)

At least one of these must be set:

### `GITHUB_TOKEN`
- **Type**: `str | None`
- **Required**: ⚠️ If not using BYOK
- **Default**: `None`
- **Description**: GitHub token for Copilot auth (PAT with `copilot` scope or GitHub App token)
- **Security**: Authorizes Copilot API access
- **Validation**: Cross-checked with `COPILOT_PROVIDER_TYPE` in `_check_auth()`

### `COPILOT_PROVIDER_TYPE`
- **Type**: `str | None`
- **Required**: ⚠️ If not using GitHub Copilot
- **Default**: `None`
- **Options**: `"azure"`, `"openai"`, or `None` for Copilot
- **Description**: BYOK provider type (Bring Your Own Key)

### `COPILOT_PROVIDER_BASE_URL`
- **Type**: `str | None`
- **Required**: ❌ No (required if `COPILOT_PROVIDER_TYPE` is set)
- **Default**: `None`
- **Description**: BYOK provider base URL (e.g., `https://api.openai.com/v1`)

### `COPILOT_PROVIDER_API_KEY`
- **Type**: `str | None`
- **Required**: ❌ No (required if `COPILOT_PROVIDER_TYPE` is set)
- **Default**: `None`
- **Description**: BYOK provider API key
- **Security**: Stored in Kubernetes Secret, passed as env var

### `COPILOT_MODEL`
- **Type**: `str`
- **Required**: ❌ No
- **Default**: `"gpt-4"`
- **Description**: Model to use for reviews and coding tasks

---

## Server Settings

### `HOST`
- **Type**: `str`
- **Required**: ❌ No
- **Default**: `"0.0.0.0"`
- **Description**: Server bind host

### `PORT`
- **Type**: `int`
- **Required**: ❌ No
- **Default**: `8000`
- **Description**: Server bind port

### `LOG_LEVEL`
- **Type**: `str`
- **Required**: ❌ No
- **Default**: `"info"`
- **Options**: `"debug"`, `"info"`, `"warning"`, `"error"`
- **Description**: Log level for structlog

### `AGENT_GITLAB_USERNAME`
- **Type**: `str | None`
- **Required**: ❌ No
- **Default**: `None`
- **Description**: Agent's GitLab username for loop prevention (skips self-authored `/copilot` notes)
- **Example**: `"copilot-agent"`

### `CLONE_DIR`
- **Type**: `str | None`
- **Required**: ❌ No
- **Default**: `None` (uses system temp dir)
- **Description**: Base directory for repo clones (useful for persistent volumes)

---

## Task Execution

### `TASK_EXECUTOR`
- **Type**: `Literal["local", "kubernetes"]`
- **Required**: ❌ No
- **Default**: `"local"`
- **Options**: `"local"` (in-process), `"kubernetes"` (K8s Jobs)
- **Description**: Task executor backend

---

## Kubernetes Executor Settings

Only used when `TASK_EXECUTOR=kubernetes`.

### `K8S_NAMESPACE`
- **Type**: `str`
- **Required**: ❌ No
- **Default**: `"default"`
- **Description**: Kubernetes namespace for Jobs

### `K8S_JOB_IMAGE`
- **Type**: `str`
- **Required**: ⚠️ Yes if `TASK_EXECUTOR=kubernetes`
- **Default**: `""`
- **Description**: Docker image for Job pods (must include agent code)
- **Example**: `"ghcr.io/peteroden/gitlab-copilot-agent:latest"`

### `K8S_JOB_CPU_LIMIT`
- **Type**: `str`
- **Required**: ❌ No
- **Default**: `"1"`
- **Description**: CPU limit for Job pods (K8s resource format)
- **Example**: `"2"`, `"500m"`

### `K8S_JOB_MEMORY_LIMIT`
- **Type**: `str`
- **Required**: ❌ No
- **Default**: `"1Gi"`
- **Description**: Memory limit for Job pods (K8s resource format)
- **Example**: `"2Gi"`, `"512Mi"`

### `K8S_JOB_TIMEOUT`
- **Type**: `int`
- **Required**: ❌ No
- **Default**: `600`
- **Description**: Job timeout in seconds (10 minutes)

---

## State Backend

### `STATE_BACKEND`
- **Type**: `Literal["memory", "redis"]`
- **Required**: ❌ No
- **Default**: `"memory"`
- **Options**: `"memory"` (single pod only), `"redis"` (distributed)
- **Description**: State backend for locks and deduplication

### `REDIS_URL`
- **Type**: `str | None`
- **Required**: ⚠️ Yes if `STATE_BACKEND=redis`
- **Default**: `None`
- **Description**: Redis connection URL
- **Format**: `redis://host:port/db` or `redis://:password@host:port/db`
- **Example**: `"redis://redis-service:6379/0"`
- **Validation**: Required when `STATE_BACKEND=redis` (enforced by `_check_auth()`)

---

## Project Allowlist

### `GITLAB_PROJECTS`
- **Type**: `str | None`
- **Required**: ⚠️ Yes if `GITLAB_POLL=true`
- **Default**: `None`
- **Description**: Comma-separated GitLab project paths or IDs to scope webhook and poller
- **Format**: `"group/project1,group/project2,12345"`
- **Validation**: Each entry resolved to numeric ID at startup; required when `GITLAB_POLL=true`

---

## GitLab Polling

### `GITLAB_POLL`
- **Type**: `bool`
- **Required**: ❌ No
- **Default**: `False`
- **Description**: Enable GitLab API polling for MR and note discovery (alternative to webhooks)

### `GITLAB_POLL_INTERVAL`
- **Type**: `int`
- **Required**: ❌ No
- **Default**: `30`
- **Description**: Polling interval in seconds

---

## Jira Integration

All optional — service runs review-only without these.

### `JIRA_URL`
- **Type**: `str | None`
- **Required**: ❌ No
- **Default**: `None`
- **Description**: Jira instance URL (e.g., `https://company.atlassian.net`)

### `JIRA_EMAIL`
- **Type**: `str | None`
- **Required**: ❌ No
- **Default**: `None`
- **Description**: Jira user email for basic auth

### `JIRA_API_TOKEN`
- **Type**: `str | None`
- **Required**: ❌ No
- **Default**: `None`
- **Description**: Jira API token or PAT
- **Security**: Stored in Kubernetes Secret

### `JIRA_TRIGGER_STATUS`
- **Type**: `str`
- **Required**: ❌ No
- **Default**: `"AI Ready"`
- **Description**: Jira status that triggers the agent

### `JIRA_IN_PROGRESS_STATUS`
- **Type**: `str`
- **Required**: ❌ No
- **Default**: `"In Progress"`
- **Description**: Status to transition to after agent picks up issue

### `JIRA_POLL_INTERVAL`
- **Type**: `int`
- **Required**: ❌ No
- **Default**: `30`
- **Description**: Poll interval in seconds

### `JIRA_PROJECT_MAP`
- **Type**: `str | None`
- **Required**: ❌ No
- **Default**: `None`
- **Description**: JSON string mapping Jira project keys to GitLab projects
- **Format**: See [Jira Project Map Format](#jira-project-map-format) below

**Jira Activation Logic**: All of `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, and `JIRA_PROJECT_MAP` must be set. The `Settings.jira` property returns `JiraSettings` if all required fields are present, else `None`.

---

## Telemetry

### `OTEL_EXPORTER_OTLP_ENDPOINT`
- **Type**: `str` (not in Settings model, read directly by telemetry.py)
- **Required**: ❌ No
- **Default**: Unset (telemetry disabled)
- **Description**: OTLP gRPC endpoint for traces, metrics, and logs
- **Example**: `"http://otel-collector:4317"`
- **Behavior**: If unset, `init_telemetry()` is a no-op

### `SERVICE_VERSION`
- **Type**: `str` (not in Settings model)
- **Required**: ❌ No
- **Default**: `"0.1.0"`
- **Description**: Service version for OTEL resource attributes

### `DEPLOYMENT_ENV`
- **Type**: `str` (not in Settings model)
- **Required**: ❌ No
- **Default**: `""`
- **Description**: Deployment environment label (e.g., "production", "staging")

---

## Jira Project Map Format

JSON object with top-level `"mappings"` key:

```json
{
  "mappings": {
    "PROJ": {
      "gitlab_project_id": 42,
      "clone_url": "https://gitlab.example.com/group/project.git",
      "target_branch": "main"
    },
    "DEMO": {
      "gitlab_project_id": 99,
      "clone_url": "https://gitlab.example.com/team/demo.git",
      "target_branch": "develop"
    }
  }
}
```

**Fields**:
- **Jira project key** (e.g., `"PROJ"`): Top-level keys in `mappings`
- **gitlab_project_id** (`int`): GitLab project ID
- **clone_url** (`str`): HTTPS clone URL
- **target_branch** (`str`): Default MR target branch (default: `"main"`)

**Parsing**: Loaded via `ProjectMap.model_validate_json()` in `main.py`.

---

## Validation Summary

| Validator | Condition | Error |
|-----------|-----------|-------|
| `_check_auth()` | Neither `GITHUB_TOKEN` nor `COPILOT_PROVIDER_TYPE` set | "Either GITHUB_TOKEN or COPILOT_PROVIDER_TYPE must be set" |
| `_check_auth()` | `STATE_BACKEND=redis` and `REDIS_URL` is None | "REDIS_URL is required when STATE_BACKEND=redis" |
| `_check_auth()` | `GITLAB_POLL=true` and `GITLAB_PROJECTS` is empty | "GITLAB_PROJECTS is required when GITLAB_POLL=true" |

---

## Configuration Examples

### Minimal (Webhook-Only, In-Memory)
```bash
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=glpat-xxxxx
GITLAB_WEBHOOK_SECRET=my-secret
GITHUB_TOKEN=ghp_xxxxx
```

### Webhook + GitLab Poller (In-Memory)
```bash
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=glpat-xxxxx
GITLAB_WEBHOOK_SECRET=my-secret
GITHUB_TOKEN=ghp_xxxxx
GITLAB_POLL=true
GITLAB_POLL_INTERVAL=60
GITLAB_PROJECTS="group/project1,group/project2"
AGENT_GITLAB_USERNAME=copilot-agent
```

### Production (K8s Jobs + Redis + Jira + OTEL)
```bash
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=glpat-xxxxx
GITLAB_WEBHOOK_SECRET=my-secret
GITHUB_TOKEN=ghp_xxxxx

TASK_EXECUTOR=kubernetes
K8S_NAMESPACE=copilot-agent
K8S_JOB_IMAGE=ghcr.io/peteroden/gitlab-copilot-agent:v1.0.0
K8S_JOB_CPU_LIMIT=2
K8S_JOB_MEMORY_LIMIT=2Gi
K8S_JOB_TIMEOUT=900

STATE_BACKEND=redis
REDIS_URL=redis://redis-service:6379/0

GITLAB_POLL=true
GITLAB_POLL_INTERVAL=30
GITLAB_PROJECTS="group/infra,group/app"
AGENT_GITLAB_USERNAME=copilot-agent

JIRA_URL=https://company.atlassian.net
JIRA_EMAIL=bot@example.com
JIRA_API_TOKEN=xxxxx
JIRA_TRIGGER_STATUS="AI Ready"
JIRA_IN_PROGRESS_STATUS="In Progress"
JIRA_POLL_INTERVAL=30
JIRA_PROJECT_MAP='{"mappings":{"PROJ":{"gitlab_project_id":42,"clone_url":"https://gitlab.example.com/group/project.git","target_branch":"main"}}}'

OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
SERVICE_VERSION=1.0.0
DEPLOYMENT_ENV=production
```

### BYOK (Azure OpenAI)
```bash
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=glpat-xxxxx
GITLAB_WEBHOOK_SECRET=my-secret

COPILOT_PROVIDER_TYPE=azure
COPILOT_PROVIDER_BASE_URL=https://my-resource.openai.azure.com
COPILOT_PROVIDER_API_KEY=xxxxx
COPILOT_MODEL=gpt-4
```

---

## Security Considerations

### Secrets
All tokens/keys should be:
- Stored in Kubernetes Secrets
- Mounted as environment variables (Helm chart handles this)
- Never committed to code
- Rotated regularly

### Least Privilege
- **GITLAB_TOKEN**: Scope to specific projects if possible (use project access tokens)
- **GITHUB_TOKEN**: Minimal scope (`copilot` only)
- **JIRA_API_TOKEN**: Read issue, transition, add comment (no admin)

### Network Isolation
- Redis: no auth (in-cluster only, use NetworkPolicy to restrict access)
- OTEL Collector: internal endpoint only

---

## Helm Values Mapping

Helm `values.yaml` maps to env vars via `configmap.yaml` and `secret.yaml`:

| Helm Value | Env Var | Secret? |
|------------|---------|---------|
| `gitlab.url` | `GITLAB_URL` | ❌ |
| `gitlab.token` | `GITLAB_TOKEN` | ✅ |
| `gitlab.webhookSecret` | `GITLAB_WEBHOOK_SECRET` | ✅ |
| `github.token` | `GITHUB_TOKEN` | ✅ |
| `controller.copilotProviderType` | `COPILOT_PROVIDER_TYPE` | ❌ |
| `controller.copilotProviderBaseUrl` | `COPILOT_PROVIDER_BASE_URL` | ❌ |
| `controller.copilotProviderApiKey` | `COPILOT_PROVIDER_API_KEY` | ✅ |
| `controller.copilotModel` | `COPILOT_MODEL` | ❌ |
| `controller.taskExecutor` | `TASK_EXECUTOR` | ❌ |
| `controller.stateBackend` | `STATE_BACKEND` | ❌ |
| `redis.enabled` | `REDIS_URL` (auto-generated) | ❌ |
| `telemetry.otlpEndpoint` | `OTEL_EXPORTER_OTLP_ENDPOINT` | ❌ |
| `telemetry.environment` | `DEPLOYMENT_ENV` | ❌ |
| `jira.url` | `JIRA_URL` | ❌ |
| `jira.email` | `JIRA_EMAIL` | ✅ |
| `jira.apiToken` | `JIRA_API_TOKEN` | ✅ |
| `jira.projectMap` | `JIRA_PROJECT_MAP` | ❌ |
| `jira.triggerStatus` | `JIRA_TRIGGER_STATUS` | ❌ |
| `jira.inProgressStatus` | `JIRA_IN_PROGRESS_STATUS` | ❌ |
| `jira.pollInterval` | `JIRA_POLL_INTERVAL` | ❌ |

See `helm/gitlab-copilot-agent/values.yaml` for full reference.
