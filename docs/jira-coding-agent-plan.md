# Jira-Driven Coding Agent — Implementation Plan

## Problem

The service currently only reviews MRs reactively. Teams want the agent to also **implement** Jira issues — pick up a task, write code, create an MR, and iterate based on feedback. This turns it into a lightweight coding agent for Jira + GitLab workflows.

## Deployment Modes

The service supports three independent deployment modes via config:

| Mode | Required config | What runs |
|------|----------------|-----------|
| **MR Review only** | `GITLAB_URL`, `GITLAB_TOKEN`, `GITLAB_WEBHOOK_SECRET` | Webhook endpoint, review engine |
| **Jira Coding Agent only** | `JIRA_URL`, `JIRA_API_TOKEN`, `JIRA_GITLAB_PROJECT_MAP` | Jira poller, coding engine |
| **Both** | All of the above | Everything |

Each mode is activated by the presence of its config. No Jira config → poller never starts, webhook still works. No GitLab webhook secret → webhook endpoint still mounts but rejects unsigned requests (existing behavior). The two features share `gitlab_client.py` and `git_operations.py` but have no coupling beyond that.

## User Flows

### Flow 1: Jira Issue → Implementation (Polling)
1. PM moves Jira issue to "AI Ready" status in Jira
2. Agent polls Jira via REST API: `status = "AI Ready" AND project IN (PROJ)`
3. Agent picks up the issue, reads title, description, acceptance criteria
4. Maps Jira project → GitLab project (via config)
5. Clones repo, creates branch `agent/<PROJ-123>/<slug>`
6. Runs Copilot SDK coding session (issue details as prompt)
7. Commits, pushes, creates GitLab MR (title: `feat(PROJ-123): <issue summary>`)
8. Updates Jira: transitions to "In Progress", comments with MR link

No Jira admin access required — only a PAT token with project read/write.

### Flow 2: MR Comment Feedback Loop
1. Reviewer comments on MR: `/agent fix the error handling in parse()`
2. GitLab fires `note` webhook (`object_kind: "note"`)
3. Agent checks `object_attributes.note` for `/agent` prefix
4. Clones/pulls the MR source branch
5. Runs Copilot SDK session with the instruction as prompt
6. Commits and pushes improvements
7. Replies on MR with summary of changes

## Acceptance Criteria

### Jira Trigger (Polling)
- [ ] Agent polls Jira on a configurable interval (default 30s) for issues in trigger status
- [ ] Agent ignores issues it has already processed (idempotent)
- [ ] Agent creates a GitLab MR with working code changes
- [ ] Branch naming: `agent/<jira-key>/<slug>`
- [ ] Jira issue transitions to "In Progress" and gets MR link comment
- [ ] Polling is resilient to Jira API errors (logs and retries next cycle)

### MR Feedback
- [ ] Agent responds to `/agent <instruction>` comments on MRs
- [ ] Agent ignores MR comments without the `/agent` prefix
- [ ] Agent commits improvements to the existing MR branch
- [ ] Agent replies with a summary of what it changed
- [ ] Existing MR review functionality is not broken

### Independence
- [ ] Service starts and reviews MRs with zero Jira config (review-only mode)
- [ ] Service starts and processes Jira issues with zero webhook secret (coding-only mode)
- [ ] Service runs both capabilities simultaneously when both configs are present
- [ ] No import or runtime dependency between `review_engine` and `coding_engine`

### Shared Repo Config
- [ ] Coding engine calls `discover_repo_config()` on the cloned target repo
- [ ] Coding session receives the target repo's skills, custom agents, and instructions
- [ ] Review engine continues to use `discover_repo_config()` as before (no regression)
- [ ] Both engines respect the same `.github/`, `.claude/`, and `AGENTS.md` conventions

## Out of Scope (v1)
- Jira webhook trigger (future enhancement — requires Jira admin access)
- Manual Jira comment triggers (`/agent` on Jira issues) — separate future issue
- Multi-commit sessions (agent makes one commit per run)
- Auto-merge on approval
- CI/CD pipeline integration
- Multi-instance / distributed locking (single instance MVP)

## Independence Guarantee

The two capabilities must never depend on each other:

1. **Config**: All Jira settings are optional with sensible defaults. Missing `JIRA_URL` → Jira poller does not start. Missing `GITLAB_WEBHOOK_SECRET` → webhook still mounts but rejects (existing). Neither blocks the other.
2. **Startup**: `main.py` lifespan conditionally starts the poller only when Jira config is present. The webhook router always mounts (it's cheap and already guarded).
3. **Shared code**: `gitlab_client.py` (clone, MR API), `git_operations.py` (branch, commit, push), and `repo_config.py` (agent/skill/instruction discovery) are shared. Both engines call `discover_repo_config()` on the cloned repo to load the target project's conventions. No shared mutable state or locks between review and coding flows.
4. **Tests**: Each module has its own tests. Integration tests for Jira coding flow do not import or depend on review engine, and vice versa.
5. **Docker/deploy**: Single image, single process. Mode is determined entirely by env vars at startup.

## Architecture

### New Modules
| Module | Responsibility |
|--------|---------------|
| `copilot_session.py` | Shared SDK lifecycle: client init, repo config discovery, session config assembly (skills/agents/instructions/provider), event collection, cleanup. Both engines call `run_copilot_session(settings, repo_path, system_prompt, user_prompt) → str`. |
| `jira_poller.py` | Background task: poll Jira for issues in trigger status |
| `jira_models.py` | Pydantic models for Jira API responses |
| `jira_client.py` | Jira REST API: search issues, transition, add comment |
| `coding_engine.py` | Coding-specific system prompt and prompt builder. Calls `run_copilot_session()`. |
| `git_operations.py` | `GitRepo` class: branch, commit, push, pull |
| `coding_orchestrator.py` | Jira issue → clone → code → MR → Jira update |
| `comment_handler.py` | MR `/agent` comment → code update → reply |
| `project_mapping.py` | Jira project key → GitLab project ID + clone URL config |

### Modified Modules
| Module | Change |
|--------|--------|
| `config.py` | Add optional `JiraSettings` group (url, email, token, trigger status, poll interval, project mapping). Present → poller starts. Absent → review-only mode. |
| `review_engine.py` | Refactor to call `run_copilot_session()` from `copilot_session.py` instead of inline SDK wiring. Retains only the review system prompt and `build_review_prompt()`. |
| `main.py` | Conditionally start/stop Jira poller in lifespan based on `settings.jira` presence |
| `webhook.py` | Add note event handler for `/agent` commands (MR feedback loop — independent of Jira) |
| `gitlab_client.py` | Add `create_merge_request()`, `post_mr_comment()` (shared by both capabilities) |

### Key Design Decisions
1. **Polling over webhooks** — no Jira admin access needed, works behind firewalls, catches up on restart. Webhook can be added later behind the same `JiraIssueHandler` interface.
2. **Shared Copilot SDK session runner** — extract the common SDK lifecycle (client init → `discover_repo_config` → build `SessionConfig` with skills/agents/instructions/provider → create session → collect messages → cleanup) into a `copilot_session.py` module. Both `review_engine.py` and `coding_engine.py` call this with different system prompts and user prompts. No duplicated SDK wiring.
3. **Shared `repo_config.py`** — both engines load agents, skills, and instructions from the cloned target repo via `discover_repo_config()`. The coding agent respects the same project conventions as the review agent.
4. **`GitRepo` abstraction** in `git_operations.py` — reusable for branch/commit/push (existing clone stays in gitlab_client for now)
5. **Branch prefix `agent/`** — review agent can ignore agent-created MRs, prevents conflicts
6. **Per-repo locking** — prevent concurrent coding sessions on the same repo
7. **Project mapping via env var** — JSON config `JIRA_GITLAB_PROJECT_MAP='{"PROJ": {"gitlab_project_id": 12345, "clone_url": "https://..."}}'`
8. **Issue handler abstraction** — `coding_orchestrator.py` accepts a dataclass (not a Jira-specific payload), so the same flow works for future webhook or manual triggers

## Epic Breakdown

### Phase 1: Foundation (3 PRs)

**PR 1: Git operations abstraction** (~150 lines)
- Create `git_operations.py` with `GitRepo` class: `create_branch()`, `commit()`, `push()`
- Tests for each operation using local bare repos
- AC: All git operations work on local test repos

**PR 2: Jira client and models** (~150 lines)
- Create `jira_models.py`: Pydantic models for Jira API issue responses
- Create `jira_client.py`: search issues by JQL, get issue details, transition issue, add comment
- Auth: basic auth with email + API token (Cloud) or PAT (Server/DC)
- Create `project_mapping.py`: config-based mapping
- Tests with mocked Jira API
- AC: Can search for issues in trigger status, transition issue, add comment

**PR 3: Jira poller and config** (~150 lines)
- Add optional `JiraSettings` model to `config.py` (nested Pydantic model, all fields optional at top level)
- `settings.jira` is `None` when no Jira env vars are set → service runs as review-only
- Create `jira_poller.py`: background asyncio task, polls on interval, calls handler
- Wire into `main.py` lifespan: `if settings.jira: start_poller()`
- Poller uses a `CodingTaskHandler` Protocol so webhook trigger can be added later
- AC: Without Jira config, service starts normally (review-only). With Jira config, poller runs.

### Phase 2: Jira → Code Flow (2 PRs)

**PR 4: Copilot session runner and coding engine** (~180 lines)
- Extract `copilot_session.py` from `review_engine.py`: `run_copilot_session(settings, repo_path, system_prompt, user_prompt, timeout) → str`
  - Handles: client init, `discover_repo_config()`, `SessionConfig` assembly (skills, agents, instructions, provider/model), event collection, nested try/finally cleanup
- Refactor `review_engine.py` to call `run_copilot_session()` (retains `SYSTEM_PROMPT` and `build_review_prompt()` only)
- Create `coding_engine.py`: coding-specific `SYSTEM_PROMPT` + `build_coding_prompt()`, calls `run_copilot_session()`
- Existing review tests pass unchanged (refactor is internal)
- New tests for `copilot_session.py` and `coding_engine.py` with mocked SDK
- AC: Review engine works identically after refactor. Coding engine produces output using same SDK wiring and repo config.

**PR 5: Coding orchestrator** (~180 lines)
- Create `coding_orchestrator.py`: wire Jira → clone → code → MR → Jira update
- Add `create_merge_request()` to `gitlab_client.py`
- Idempotency: check if branch already exists before creating
- AC: End-to-end flow from Jira webhook to MR creation

### Phase 3: MR Feedback Loop (1 PR)

**PR 6: Comment handler** (~180 lines)
- Create `comment_handler.py`: parse `/agent` command, run coding session, commit, reply
- Extend `webhook.py` to handle `note` events
- Add `post_mr_comment()` to `gitlab_client.py`
- AC: `/agent` comment triggers code changes and reply

### Phase 4: Hardening (1 PR)

**PR 7: Deduplication, locking, observability** (~150 lines)
- Polling deduplication (track processed Jira issue keys in memory, survive restarts via Jira status check)
- Per-repo asyncio.Lock to prevent concurrent coding sessions
- Structured logging for full coding flow
- AC: Duplicate poll results are idempotent, concurrent requests don't conflict

## Risks

| Risk | Mitigation |
|------|-----------|
| Agent produces broken code | MR is always draft; human must review and approve |
| Copilot SDK timeout on complex issues | 300s timeout (same as review), log and comment on Jira |
| Jira API rate limiting | Respect rate limits, exponential backoff, configurable poll interval |
| Branch conflicts between coding and review | `agent/` branch prefix, review agent can filter |
| Duplicate processing | Transition to "In Progress" immediately; track processed keys in memory |

## Test Environment

Existing accounts available for E2E testing:
- **GitLab**: `gitlab.com` — existing test project (`peteroden/calculator-test`, MR `!1`). Create a second test project for coding agent MR creation tests.
- **Jira**: existing account — create a test project with "AI Ready" status in workflow for poller testing.

Each PR will include unit tests (mocked) and can be validated with a live E2E test against these accounts before merge.

## Open Questions (need Product input)

1. Should the MR be created as Draft by default?
2. Should the agent add an `ai-generated` label to MRs?
3. What's the default target branch — always `main` or configurable?
4. Should the agent auto-assign the MR to the Jira issue assignee?

## Future Enhancements

- **GitLab API polling for MR reviews** — poll GitLab for new/updated MRs via REST API as an alternative to webhooks. No GitLab admin access needed, works behind firewalls, supports self-hosted instances without webhook configuration. Webhook remains the primary trigger; polling is an additional option for environments where webhooks aren't feasible.
- **Jira webhook trigger** — instant triggering without polling, requires Jira admin access
- **Jira comment trigger** — `/agent` comment on Jira issues
- **`.claude/rules/*.md` support** — modular, path-scoped instruction files
