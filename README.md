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

The service uses **bubblewrap (bwrap)** to isolate the Copilot SDK subprocess in a read-only filesystem sandbox with a throwaway `/tmp` and `/home`. This prevents the agent from modifying system directories or persisting state outside the cloned repo.

When running in Docker, the container needs:

```bash
--security-opt seccomp=unconfined
```

**You do NOT need** `--cap-add=SYS_ADMIN`.

If bwrap is unavailable or sandbox creation fails, the service automatically falls back to unsandboxed execution (and logs a warning). On macOS and some CI environments, bwrap isn't available — the fallback ensures the service still works.

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

**Sandbox creation failed** (bwrap errors in logs)
- Ensure Docker is running with `--security-opt seccomp=unconfined`
- The service will fall back to unsandboxed execution — check logs for fallback warnings
- On macOS/Windows, bwrap isn't available — this is expected (uses NoopSandbox)

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

## Architecture

See `docs/PLAN.md` for full implementation plan and `docs/adr/` for architecture decisions.

```
GitLab Webhook → FastAPI /webhook → Clone repo → Copilot agent review → Parse output → Post inline comments + summary
```
