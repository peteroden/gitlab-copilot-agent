# Implementation Plan: GitLab Copilot Agent MR Review Service

## Problem

Build a Python service that automatically reviews GitLab Merge Requests using the GitHub Copilot SDK. The service receives GitLab webhooks when MRs are created/updated, clones the repo, feeds the diff + repo context + project governance rules to a Copilot agent session, and posts review comments (inline + summary) back to the MR.

## Approach

- **Framework**: FastAPI (async-native, matches Copilot SDK's async API)
- **AI Engine**: GitHub Copilot SDK (`github-copilot-sdk`) with configurable auth (Copilot subscription or Azure OpenAI BYOK)
- **GitLab Integration**: `python-gitlab` for API calls (post comments, fetch MR metadata), `git clone` for full repo access
- **Deployment**: Azure App Service (Linux, Python)
- **Review Strategy**: Clone repo to temp dir → provide agent with file system access → agent reads diff + project files → structured review output → post inline comments on diff lines + general summary comment

## Architecture

```
GitLab Webhook (MR open/update)
  → FastAPI /webhook endpoint
  → Validate secret token
  → Extract MR metadata (project, branch, diff)
  → Clone repo to temp dir (source branch)
  → Build Copilot session with:
      - System prompt from .github/copilot-instructions.md
      - Code review skill (new, based on OWASP + governance)
      - Custom tools: read_file, list_files, get_mr_diff
  → Agent performs review
  → Parse agent output into structured comments
  → Post to GitLab: inline discussion threads + summary note
  → Clean up temp dir
```

## Agent Assignments

| Step | Agent | What |
|------|-------|------|
| Research docs | **Architect** | Capture Copilot SDK and GitLab API research into `docs/research/` |
| ADR | **Architect** | Write ADR for service architecture (webhook flow, agent session design, comment posting strategy) |
| Python instructions | **Architect** | Author `.github/instructions/python.instructions.md` |
| PR 1–6 implementation | **Developer** | Implement each PR following the ADR and instructions |
| PR sequencing | **Orchestrator** | Create worktrees, assign PRs, enforce governance, manage merge order |
| Scope validation | **Product** | Validate acceptance criteria before each PR is opened |

The Architect produces three artifacts before any code is written: research docs, the ADR, and the Python instruction file. The Orchestrator then sequences the Developer work.

## Progress

### Step 0a: Research docs _(Architect)_
- [x] `docs/research/copilot-sdk-python.md` — Copilot SDK Python API reference:
  - `CopilotClient` init options (`cli_path`, `cli_url`, `github_token`, `use_logged_in_user`)
  - `create_session` options (`model`, `tools`, `system_message`, `streaming`, `provider`, `hooks`)
  - Custom tools via `@define_tool` + Pydantic models, and low-level `Tool()` API
  - BYOK provider config (`type`, `base_url`, `api_key`, `azure.api_version`)
  - Event types: `assistant.message`, `assistant.message_delta`, `session.idle`
  - Session hooks: `on_pre_tool_use`, `on_post_tool_use`, `on_error_occurred`
  - Requires Copilot CLI binary in PATH
- [x] `docs/research/gitlab-api.md` — GitLab API v4 reference for this project:
  - Webhook payload: `object_kind`, `object_attributes.action` (`open`, `update`), `object_attributes.iid`, `project.id`, source/target branches, SHAs
  - `GET /projects/:id/merge_requests/:iid/changes` — returns `changes[]` with `old_path`, `new_path`, `diff` (unified diff hunks)
  - `POST /projects/:id/merge_requests/:iid/notes` — post general MR comment (`body`)
  - `POST /projects/:id/merge_requests/:iid/discussions` — post inline thread with `position` object (`base_sha`, `start_sha`, `head_sha`, `position_type`, `new_line`, `old_path`, `new_path`)
  - Auth: `PRIVATE-TOKEN` header or `python-gitlab` client
  - Webhook secret: `X-Gitlab-Token` header

### Step 0b: Architecture ADR _(Architect)_
- [x] `docs/adr/0001-mr-review-service-architecture.md` — 7 decisions documented:
  - FastAPI + async over Flask (matches SDK)
  - Clone-to-temp-dir over API-only context (agent gets real file access)
  - Copilot SDK with custom tools over raw LLM API (leverages agent runtime)
  - Background task over synchronous webhook response (timeout avoidance)
  - Inline + summary comments over summary-only (richer feedback)
  - Configurable auth: GitHub Copilot subscription + Azure OpenAI BYOK
  - Structured JSON output from agent with free-text fallback

### Step 0c: Python instruction file _(Architect)_
- [x] `.github/instructions/python.instructions.md` — Python-specific guidance covering:
  async patterns, error handling, Pydantic, DI, type strictness, project layout, testing

### PR 1: Project scaffold + FastAPI skeleton _(Developer)_
- [ ] `pyproject.toml` with pinned dependencies: `fastapi`, `uvicorn`, `python-gitlab`, `github-copilot-sdk`, `pydantic`, `pydantic-settings`, `structlog`
- [ ] `src/gitlab_copilot_agent/` package structure
- [ ] `src/gitlab_copilot_agent/main.py` — FastAPI app with health check endpoint
- [ ] `src/gitlab_copilot_agent/config.py` — Settings via pydantic-settings (env vars): `GITLAB_TOKEN`, `GITLAB_URL`, `GITLAB_WEBHOOK_SECRET`, `COPILOT_MODEL`, `COPILOT_PROVIDER_TYPE`, `COPILOT_PROVIDER_BASE_URL`, `COPILOT_PROVIDER_API_KEY`, `GITHUB_TOKEN`
- [ ] Update `.devcontainer/devcontainer.json` to install copilot CLI
- [ ] Update `README.md` with project description and setup
- [ ] Tests: health check endpoint, config loading

### PR 2: Webhook endpoint + GitLab event parsing _(Developer)_
- [ ] `src/gitlab_copilot_agent/webhook.py` — POST /webhook endpoint
- [ ] Verify `X-Gitlab-Token` header against configured secret
- [ ] Parse MR webhook payload (`object_kind == "merge_request"`, `action in ["open", "update"]`)
- [ ] Extract: project ID, MR IID, source branch, target branch, source/target SHAs
- [ ] `src/gitlab_copilot_agent/models.py` — Pydantic models for webhook payload (relevant fields only)
- [ ] Return 200 immediately, queue review as background task
- Reference: `docs/research/gitlab-api.md` for webhook payload structure
- [ ] Tests: webhook validation, payload parsing, rejection of non-MR events

### PR 3: GitLab client — repo cloning + diff fetching _(Developer)_
- [ ] `src/gitlab_copilot_agent/gitlab_client.py`
- [ ] Clone repo to temp directory (shallow clone of source branch, then fetch target branch for diff context)
- [ ] Fetch MR diff via GitLab API (`/projects/:id/merge_requests/:iid/changes`)
- [ ] Fetch MR metadata (title, description)
- [ ] Clean up temp directory helper
- Reference: `docs/research/gitlab-api.md` for API endpoints and response shapes
- [ ] Tests: mock GitLab API responses, verify clone commands

### PR 4: Copilot review engine _(Developer)_
- [ ] `src/gitlab_copilot_agent/review_engine.py`
- [ ] Initialize `CopilotClient` with configurable provider (GitHub auth or Azure OpenAI BYOK)
- [ ] Create session with system prompt built from `.github/copilot-instructions.md` + code-review skill
- [ ] Define custom tools for the agent:
  - `read_file(path)` — read a file from the cloned repo
  - `list_directory(path)` — list files in the cloned repo
  - `get_mr_diff()` — return the MR diff
  - `get_mr_info()` — return MR title, description, changed files list
- [ ] Send review prompt with diff context
- [ ] Collect agent response
- Reference: `docs/research/copilot-sdk-python.md` for SDK API, tool definitions, session config
- [ ] Tests: mock Copilot SDK, verify prompt construction, tool definitions

### PR 5: Review output parsing + GitLab comment posting _(Developer)_
- [ ] `src/gitlab_copilot_agent/comment_parser.py` — Parse agent output into structured review comments (file, line, comment text, severity)
- [ ] `src/gitlab_copilot_agent/comment_poster.py` — Post comments to GitLab:
  - Inline discussion threads via `/projects/:id/merge_requests/:iid/discussions` with position data
  - Summary note via `/projects/:id/merge_requests/:iid/notes`
- [ ] Handle edge cases: file not in diff, line not in diff range (fall back to general comment)
- Reference: `docs/research/gitlab-api.md` for discussion position object structure
- [ ] Tests: output parsing, comment posting with mocked GitLab API

### PR 6: End-to-end integration + Azure deployment config _(Developer)_
- [ ] Wire webhook → clone → review → post in `src/gitlab_copilot_agent/orchestrator.py`
- [ ] Background task execution with proper error handling and logging
- [ ] `Dockerfile` for Azure App Service
- [ ] `startup.sh` for Azure
- [ ] Integration test with full flow (mocked externals)
- [ ] Update `README.md` with deployment instructions and environment variable reference

## Execution Sequence

```
Architect: Step 0a (research docs) + Step 0b (ADR) + Step 0c (python instructions)
    ↓
Orchestrator: Create worktrees, assign PR 1
    ↓
Developer: PR 1 (scaffold)
    ↓
Orchestrator: Assign PRs 2, 3, 4 (can parallelize — independent after PR 1)
    ↓
Developer: PR 2 (webhook) | PR 3 (gitlab client) | PR 4 (review engine)
    ↓
Developer: PR 5 (comment posting) — depends on PR 3 + PR 4
    ↓
Developer: PR 6 (integration + deployment) — depends on all above
    ↓
Product: Validate acceptance criteria across all PRs
```

## Local Testing

The webhook endpoint requires a publicly reachable URL for GitLab to send events. Since local development machines typically lack a public IP, use a tunnel service:

- **ngrok** (recommended): `ngrok http 8000` → configure the generated URL as the GitLab webhook endpoint
- Alternative: any similar tunnel tool (e.g., Cloudflare Tunnel, localtunnel)

The devcontainer should include ngrok or document its installation. The `GITLAB_WEBHOOK_SECRET` should still be validated even during local testing.

## Key Design Decisions

1. **Clone vs API fetch**: Clone to temp dir. The Copilot agent gets real file system access via custom tools, enabling it to browse the repo naturally.
2. **Structured output**: Agent returns review in a defined format (JSON with file/line/comment), parsed into GitLab API calls. Fallback: if agent returns free-text, post as a single summary comment.
3. **Background processing**: Webhook returns 200 immediately; review runs as a FastAPI background task. Long reviews (large diffs) won't timeout the webhook.
4. **Auth flexibility**: Config supports both GitHub Copilot subscription auth and Azure OpenAI BYOK via environment variables.

## Assumptions

1. The Copilot CLI binary will be available in the Azure App Service container (installed in Dockerfile).
2. The GitLab instance is accessible from Azure (network connectivity).
3. The GitLab token has API access to read repos, read MRs, and post comments/discussions.
4. The `.github/` folder in this project contains the governance/instructions that the agent should follow for reviews.
5. Shallow clone is sufficient for review context (no need for full git history).
