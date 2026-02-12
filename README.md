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

## Development

```bash
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
