# GitLab Copilot Agent

Automated code review for GitLab Merge Requests, powered by the GitHub Copilot SDK.

## What It Does

Receives GitLab webhooks when MRs are opened/updated → clones the repo → runs a Copilot agent review → posts inline comments with **apply-able code suggestions** back to the MR.

## Quick Start

```bash
# 1. Clone and start devcontainer
devcontainer up --workspace-folder .

# 2. Set environment variables
export GITLAB_URL=https://gitlab.com
export GITLAB_TOKEN=glpat-...
export GITLAB_WEBHOOK_SECRET=your-secret
export GITHUB_TOKEN=$(gh auth token)

# 3. Run
devcontainer exec --workspace-folder . uv run uvicorn gitlab_copilot_agent.main:app --port 8000
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITLAB_URL` | ✅ | — | GitLab instance URL |
| `GITLAB_TOKEN` | ✅ | — | GitLab API token (needs `api` scope) |
| `GITLAB_WEBHOOK_SECRET` | ✅ | — | Secret for validating webhook payloads |
| `GITHUB_TOKEN` | ✅* | — | GitHub token for Copilot auth |
| `COPILOT_MODEL` | — | `gpt-4` | Model for reviews |
| `COPILOT_PROVIDER_TYPE` | — | `None` | BYOK provider: `azure`, `openai`, or omit for Copilot |
| `COPILOT_PROVIDER_BASE_URL` | — | `None` | BYOK provider endpoint |
| `COPILOT_PROVIDER_API_KEY` | — | `None` | BYOK provider API key |
| `HOST` | — | `0.0.0.0` | Server bind host |
| `PORT` | — | `8000` | Server bind port |
| `LOG_LEVEL` | — | `info` | Log level |

*`GITHUB_TOKEN` is required when using GitHub Copilot auth. Not needed for BYOK providers.

## GitLab Webhook Setup

1. Go to your GitLab project → **Settings** → **Webhooks**
2. Set the URL to `https://your-host/webhook`
3. Set the secret token to match `GITLAB_WEBHOOK_SECRET`
4. Check **Merge request events**
5. Save

The service needs a publicly reachable URL. For local dev, use [ngrok](https://ngrok.com): `ngrok http 8000`.

## Jira Integration

The service can **optionally** poll Jira for issues and automatically create branches + MRs for agent review. All Jira env vars are optional — the service runs in webhook-only mode if they're omitted.

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `JIRA_URL` | ✅* | — | Jira instance URL (e.g., `https://yourcompany.atlassian.net`) |
| `JIRA_EMAIL` | ✅* | — | Jira user email for basic auth |
| `JIRA_API_TOKEN` | ✅* | — | Jira API token or personal access token |
| `JIRA_PROJECT_MAP` | ✅* | — | JSON mapping Jira project keys to GitLab projects |
| `JIRA_TRIGGER_STATUS` | — | `AI Ready` | Jira issue status that triggers the agent |
| `JIRA_IN_PROGRESS_STATUS` | — | `In Progress` | Status to transition to after pickup |
| `JIRA_POLL_INTERVAL` | — | `30` | Polling interval in seconds |

*All four are required to enable Jira polling. If any are missing, the poller is disabled.

### Example `JIRA_PROJECT_MAP`

```json
{
  "PROJ": {
    "gitlab_project": "myorg/myrepo",
    "target_branch": "main"
  },
  "DEMO": {
    "gitlab_project": "demos/example",
    "target_branch": "develop"
  }
}
```

### How It Works

1. **Polls JQL**: Every `JIRA_POLL_INTERVAL` seconds, queries Jira for issues in `JIRA_TRIGGER_STATUS`
2. **Transitions**: Moves picked-up issues to `JIRA_IN_PROGRESS_STATUS`
3. **Creates branches**: Creates a new branch from `target_branch` (named `{project-key}-{issue-number}-{sanitized-title}`)
4. **Creates MRs**: Opens an MR from the new branch, targeting `target_branch`
5. **Triggers review**: The MR triggers the normal webhook review flow

## Repo-Level Configuration

The agent automatically loads project-specific config from the reviewed repo:

**Skills and agents** (from `.github/` and `.claude/`):

| Path | What | How |
|---|---|---|
| `.github/skills/*/SKILL.md` | Skills | SDK-native `skill_directories` |
| `.claude/skills/*/SKILL.md` | Skills | SDK-native `skill_directories` |
| `.github/agents/*.agent.md` | Custom agents | SDK-native `custom_agents` (YAML frontmatter) |
| `.claude/agents/*.agent.md` | Custom agents | SDK-native `custom_agents` (YAML frontmatter) |

**Instructions** (all discovered, concatenated into system message):

| Path | Standard | Scope |
|---|---|---|
| `.github/copilot-instructions.md` | GitHub Copilot | Project-wide |
| `.github/instructions/*.md` | GitHub Copilot | Per-language |
| `.claude/CLAUDE.md` | Claude Code | Project-wide |
| `AGENTS.md` | Universal (Copilot, Claude, Codex, Cursor, GitLab Duo) | Project root + subdirectories |
| `CLAUDE.md` | Claude Code | Project root |

Symlinked files (e.g., `ln -s AGENTS.md CLAUDE.md`) are deduplicated automatically.

## Review Output

Each review comment includes:
- Severity tag: `[ERROR]`, `[WARNING]`, or `[INFO]`
- Description of the issue
- **Inline code suggestion** (when a concrete fix exists) — click "Apply suggestion" in the GitLab UI to commit the fix

## Docker

```bash
docker build -t gitlab-copilot-agent .
docker run -p 8000:8000 \
  -e GITLAB_URL=https://gitlab.com \
  -e GITLAB_TOKEN=glpat-... \
  -e GITLAB_WEBHOOK_SECRET=secret \
  -e GITHUB_TOKEN=gho_... \
  gitlab-copilot-agent
```

## Operations

### Memory Bounds

The service uses in-memory structures that are bounded to prevent growth during long uptimes:

| Structure | Purpose | Default Limit | Eviction Strategy |
|---|---|---|---|
| `RepoLockManager` | Serializes concurrent operations on the same repo | 1,024 entries | LRU — evicts oldest idle (unlocked) lock |
| `ProcessedIssueTracker` | Prevents re-processing Jira issues within a run | 10,000 entries | Drops oldest 50% when limit is reached |

Active locks are never evicted — the lock manager allows temporary over-capacity rather than dropping in-use locks. Both limits are configurable via constructor arguments but not currently exposed as environment variables.

### Sandbox Configuration

The service isolates the Copilot SDK subprocess to prevent it from modifying system directories or persisting state outside the cloned repo. Multiple isolation methods are supported:

#### Sandbox Methods

| Method | Isolation | Setup Required | Use Case |
|--------|-----------|---------------|----------|
| `bwrap` | Process-level (namespaces, seccomp) | Linux + bubblewrap installed | Default, lightweight |
| `docker` | Container-level (Docker-in-Docker) | `docker:dind` sidecar + shared volume | Production, multi-tenant |
| `podman` | Container-level (Podman-in-Podman) | Podman machine or Linux host | Rootless container environments |
| `noop` | None | Nothing | Testing, development only |

#### Setup

**bwrap** (default):
- Pre-installed on most Linux distributions
- Set `SANDBOX_METHOD=bwrap` (or omit — it's the default)
- When running in Docker, add `--security-opt seccomp=unconfined` (you do NOT need `--cap-add=SYS_ADMIN`)

**docker** (Docker-in-Docker):

Both the service and DinD sidecar need `--privileged`. A shared volume ensures cloned repos are accessible to sandbox containers.

```bash
# Create shared resources
docker volume create workspaces
docker network create copilot-net

# Start DinD sidecar
docker run -d --name dind --privileged --network copilot-net \
  -e DOCKER_TLS_CERTDIR="" \
  -v workspaces:/data/workspaces \
  docker:dind --tls=false

# Start service
docker run -d --name copilot-agent --network copilot-net \
  -v workspaces:/data/workspaces \
  -e DOCKER_HOST=tcp://dind:2375 \
  -e CLONE_DIR=/data/workspaces \
  -e SANDBOX_METHOD=docker \
  -e SANDBOX_IMAGE=copilot-cli-sandbox:latest \
  # ... other env vars ...
  gitlab-copilot-agent
```

The entrypoint automatically builds the sandbox image inside the DinD daemon on first start.

**podman** (Podman-in-Podman):

Runs nested containers directly — no sidecar needed. Requires a host with user namespace support (Linux host or `podman machine`).

> **Note:** Podman-in-Podman does NOT work when the service runs inside Docker Desktop. Docker Desktop's LinuxKit VM lacks nested user namespace support. Use Docker DinD instead, or run via `podman machine` / native Linux.

```bash
# On a Linux host or inside podman machine:
podman run -d --name copilot-agent --privileged \
  -e SANDBOX_METHOD=podman \
  -e SANDBOX_IMAGE=copilot-cli-sandbox:latest \
  # ... other env vars ...
  gitlab-copilot-agent
```

**noop** (development only):
- Set `SANDBOX_METHOD=noop`
- No isolation — use only for local testing

#### Container Hardening

The `docker` and `podman` methods use these security flags:

`--read-only`, `--tmpfs /tmp`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--cpus=1`, `--memory=2g`, `--pids-limit=256`, `--pull=never`

## Troubleshooting

### Common Issues

**Webhook not triggering**
- Check that the webhook URL is publicly reachable (test with `curl https://your-host/webhook`)
- Verify `GITLAB_WEBHOOK_SECRET` matches the secret configured in GitLab
- Ensure "Merge request events" is enabled in the webhook settings
- Check the GitLab webhook event log (Settings → Webhooks → Recent Deliveries)

**Review posts no inline comments** (only a summary)
- Check logs for diff position validation errors — GitLab rejects positions that don't match the diff
- Ensure the MR has actual file changes (empty MRs won't have reviewable diffs)
- Verify the agent is analyzing the correct commit range

**Jira poller not processing issues**
- Verify `JIRA_PROJECT_MAP` is valid JSON and contains the Jira project key
- Check that issues are in the exact status name from `JIRA_TRIGGER_STATUS` (case-sensitive)
- Confirm the Jira user has permission to query and transition issues
- Check logs for JQL query errors or API authentication failures

**Sandbox creation failed** (startup crash)
- The service fails fast if the configured `SANDBOX_METHOD` is unavailable — there is no automatic fallback
- For `bwrap`: ensure bubblewrap is installed and Linux namespaces are available
- For `docker`/`podman`: ensure the runtime is installed, daemon is running, and sandbox image is built
- Set `SANDBOX_METHOD=noop` to explicitly disable sandboxing (development only)

**Git clone timeout** or **authentication failures**
- Verify `GITLAB_TOKEN` has `api` scope and read access to the target project
- Check network connectivity from the container to the GitLab instance
- Ensure the GitLab project URL is valid and accessible
- For self-hosted GitLab, verify SSL certificates are trusted

### Debug Logging

Enable detailed debug logs:

```bash
export LOG_LEVEL=debug
```

This shows:
- Full webhook payloads
- Git clone/checkout commands
- Copilot SDK interactions
- Diff position calculations
- API request/response details

## Development

```bash
# Install pre-commit hook (runs ruff + mypy before each commit)
ln -sf ../../scripts/pre-commit .git/hooks/pre-commit

# Run tests
devcontainer exec --workspace-folder . uv run pytest

# Lint
devcontainer exec --workspace-folder . uv run ruff check src/ tests/

# Type check
devcontainer exec --workspace-folder . uv run mypy src/
```

## Demo

See [`docs/DEMO.md`](docs/DEMO.md) for automated demo environment setup. One command provisions a GitLab repo + Jira project showcasing all agent capabilities.

## Architecture

See `docs/PLAN.md` for full implementation plan and `docs/adr/` for architecture decisions.

```
GitLab Webhook → FastAPI /webhook → Clone repo → Copilot agent review → Parse output → Post inline comments + summary
```

## Local Kubernetes Development

Run the full stack locally using [k3d](https://k3d.io) (k3s-in-Docker). All tooling runs inside the devcontainer — no host-side k8s tools required.

### Devcontainer Tooling

The devcontainer includes everything needed for local k8s development:

| Tool | Installed via | Purpose |
|------|--------------|---------|
| Docker | `docker-in-docker` devcontainer feature | Container runtime for k3d nodes |
| kubectl | `kubectl-helm-minikube` devcontainer feature | Cluster interaction |
| Helm 3 | `kubectl-helm-minikube` devcontainer feature | Chart deployment |
| k3d v5.7.5 | `postCreateCommand` install script | Local k3s cluster management |

**Docker-in-Docker** (not Docker-outside-of-Docker) is required because k3d creates its own Docker containers, networks, and port bindings. DooD would cause path and networking mismatches between the devcontainer and host.

### Dev Flow

```
┌─────────────────────────────────────────────────┐
│                 Devcontainer                     │
│                                                  │
│  1. Start devcontainer (tools auto-installed)    │
│  2. make k3d-up       → k3d cluster (~30s)       │
│  3. make k3d-build     → docker build + import    │
│  4. make k3d-deploy    → helm install             │
│  5. Edit code → make k3d-redeploy (iterate)       │
│  6. make k3d-down      → teardown when done       │
│                                                  │
│  Docker-in-Docker daemon (persists across sleep)  │
│  └── k3d cluster (k3s nodes as containers)        │
│      └── Controller pod + Redis pod + Job pods    │
└─────────────────────────────────────────────────┘
```

**Cluster lifecycle:**
- `make k3d-up` creates a fresh cluster (~30-40s first time).
- Devcontainer **sleep/restart**: cluster recovers in ~5-10s (DinD volume persists).
- Devcontainer **rebuild**: full cold start (DinD volume destroyed, re-run `make k3d-up`).

### Quick Start

```bash
cp .env.k3d.example .env.k3d   # fill in real values
make k3d-up                     # create k3d cluster
make k3d-build                  # build & import image
make k3d-deploy                 # deploy via Helm
```

### Commands

| Command            | Description                        |
|--------------------|------------------------------------|
| `make k3d-up`      | Create k3d cluster                |
| `make k3d-down`    | Delete k3d cluster                |
| `make k3d-build`   | Build image & import into cluster |
| `make k3d-deploy`  | Deploy/upgrade via Helm           |
| `make k3d-redeploy`| Rebuild + redeploy (one step)     |
| `make k3d-logs`    | Tail controller logs              |
| `make k3d-status`  | Show pods, jobs, and services     |

### Webhook Testing

The controller is exposed on `localhost:8080` via the k3d loadbalancer (override with `K3D_HOST_PORT=9000 make k3d-up`).
For direct port-forward:

```bash
kubectl port-forward svc/copilot-agent 8000:8000
```

### Live E2E Verification

After deploying to the k3d cluster, verify the full system end-to-end:

```bash
# 1. Check all pods are running
make k3d-status

# 2. Verify webhook endpoint responds
curl -s http://localhost:8080/health

# 3. Send a test webhook payload (dry run)
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: $(grep GITLAB_WEBHOOK_SECRET .env.k3d | cut -d= -f2)" \
  -d '{"object_kind": "merge_request", "event_type": "merge_request"}'

# 4. Watch logs during a live MR event
make k3d-logs
```

**Full live E2E** (requires GitLab + ngrok/tunnel):

1. Start tunnel: `ngrok http 8080` (or use VS Code port forwarding)
2. Configure webhook in GitLab project pointing to tunnel URL
3. Open/update an MR → observe agent review in `make k3d-logs`
4. Verify inline comments appear on the MR

**Jira live E2E** (requires Jira credentials in `.env.k3d`):

1. Transition a Jira issue to the trigger status
2. Watch `make k3d-logs` for poller pickup
3. Verify branch + MR creation in GitLab
4. Verify agent self-review on the new MR
