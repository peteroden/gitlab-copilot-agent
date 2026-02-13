# Config Roots and Frontmatter Refactor

## Status: Merged ✅ (PR #1)

### Completed
- [x] Replace `.gitlab/` with `.claude/` config root
- [x] Add `AGENTS.md` discovery (root + subdirectories)
- [x] Add `CLAUDE.md` discovery (root + `.claude/`)
- [x] Symlink deduplication
- [x] Replace regex with `python-frontmatter`
- [x] Pass through all `CustomAgentConfig` fields
- [x] Update README
- [x] Code review fixes (scoped instructions, config root exclusion)
- [x] Squash merged to main

---

# Code Review Remediation

## Status: Merged ✅ (PR #2)

### Completed
- [x] Sanitize GitLab token from git clone error messages
- [x] Fix session resource leak on timeout (nested try/finally)
- [x] Run Docker container as non-root user
- [x] Add 120s timeout on git clone subprocess
- [x] Clean stale `/tmp/mr-review-*` dirs on startup (shared `CLONE_DIR_PREFIX` constant)
- [x] Validate auth config at startup (github_token or provider required)
- [x] Add `--` separator to prevent git option injection
- [x] Wrap fallback comment posting in try/except
- [x] Use multi-stage Docker build for Node.js
- [x] Live E2E test passed (15 comments posted)
- [x] Squash merged to main

### Findings (ordered by severity)

#### 1. CRITICAL — Git clone leaks GitLab token in error messages
`gitlab_client.py:101` — When `git clone` fails, stderr contains the full auth URL with embedded token. This gets logged/raised, exposing credentials.
**Fix:** Sanitize stderr before including in exceptions. Replace token in URL with `***`.

#### 2. HIGH — Session resource leak on timeout
`review_engine.py:139-161` — If `asyncio.wait_for()` times out or an exception occurs, `session.destroy()` is skipped. Only `client.stop()` runs in finally.
**Fix:** Nest session destroy in its own try/finally, or move it to the outer finally block.

#### 3. HIGH — Dockerfile runs as root
No `USER` directive — the web service runs as root inside the container.
**Fix:** Add non-root user and `USER` directive before `CMD`.

#### 4. MEDIUM — No timeout on git clone subprocess
`gitlab_client.py:97` — `proc.communicate()` has no timeout. Slow/stuck clones hang indefinitely.
**Fix:** Wrap with `asyncio.wait_for(proc.communicate(), timeout=120)`.

#### 5. MEDIUM — Stale temp directories on crash
`/tmp/mr-review-*` dirs leak if the service crashes. No startup cleanup.
**Fix:** Add cleanup in lifespan startup for stale `/tmp/mr-review-*` dirs.

#### 6. MEDIUM — No auth config validation at startup
Service starts without `github_token` or valid provider config, fails at review time.
**Fix:** Add Pydantic `model_validator` to ensure auth is configured.

#### 7. LOW — Git option injection via branch names
`gitlab_client.py:93` — Branch starting with `--` could be interpreted as a git option.
**Fix:** Add `--` separator before positional args in git clone.

#### 8. LOW — Fallback comment posting stops on error
`comment_poster.py:52` — If fallback `mr.notes.create()` fails, remaining comments are skipped.
**Fix:** Wrap fallback in try/except, accumulate and log failures.

#### 9. MEDIUM — Unsafe Node.js install in Dockerfile
`curl | bash` without integrity verification.
**Fix:** Use multi-stage build with official Node.js image.

### Todos

- [ ] **sanitize-token-in-errors** — Strip GitLab token from git clone error messages
- [ ] **fix-session-leak** — Ensure session.destroy() runs on timeout/exception
- [ ] **dockerfile-nonroot** — Add non-root user to Dockerfile
- [ ] **git-clone-timeout** — Add timeout to git clone subprocess
- [ ] **cleanup-stale-temps** — Clean `/tmp/mr-review-*` on startup
- [ ] **validate-auth-config** — Fail fast if no auth configured
- [ ] **git-option-injection** — Add `--` separator in git clone args
- [ ] **fallback-comment-resilience** — Catch errors in fallback comment posting
- [ ] **dockerfile-node-install** — Use multi-stage build for Node.js

### Execution order

Items 1, 7 (git security) → 2 (session leak) → 4, 5 (operational) → 6 (config) → 8 (resilience) → 3, 9 (Dockerfile)

Expect ~150 diff lines. Single PR should suffice.

---

# Feature: Jira-Driven Coding Agent

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

---

# Future: Support `.claude/rules/*.md`

Claude Code supports modular instruction files in `.claude/rules/*.md` — these are automatically loaded as project memory. They also support path-scoping via YAML frontmatter (`paths: ["src/**/*.ts"]`), which would let us apply instructions only to relevant files in a review. Currently not loaded by our config discovery.

---

# Inline Code Suggestions Feature

## Problem

The Copilot review agent posts comments like `**[ERROR]** Function missing type hints` but doesn't propose the actual fix. GitLab supports **suggestions** — code blocks in comment bodies that render as apply-able diffs. Users can click "Apply suggestion" to commit the fix directly. We want the agent to include concrete code suggestions wherever possible.

## How GitLab Suggestions Work

No special API — suggestions are a markdown syntax in the discussion body:

````
```suggestion:-0+0
def add(a: int, b: int) -> int:
```
````

- The body goes in the same `POST /discussions` endpoint we already use
- `-0+0` means "replace just the commented line" (0 lines above, 0 below)
- `-2+1` means "replace from 2 lines above to 1 line below the commented line"
- Max 100 lines above + 100 below (201 total)
- Users see an "Apply suggestion" button in the GitLab UI
- Suggestions can be batched and applied as a single commit

## Approach

Three changes:

1. **Agent prompt** — tell the agent to include `suggestion` code blocks in its output
2. **Comment parser** — extract suggestion blocks from each comment and preserve them
3. **Comment poster** — pass suggestion markdown through to GitLab (already works since it's just body text, but need to ensure we don't strip or mangle the blocks)

## Todos

### Phase 1: Update prompt and parser

- [ ] **update-system-prompt**: Update `SYSTEM_PROMPT` in `review_engine.py` to instruct the agent to include `suggestion` blocks. Tell it: use `suggestion:-0+0` for single-line fixes, adjust offsets for multi-line. Only suggest when the fix is clear and concrete.

- [ ] **update-comment-model**: Add optional `suggestion` field to `ReviewComment` dataclass. The agent outputs `{"file": "...", "line": 42, "severity": "error", "comment": "...", "suggestion": "def add(a: int, b: int) -> int:"}` (or `suggestion_start_offset` / `suggestion_end_offset` for multi-line).

- [ ] **update-parser**: Update `parse_review()` to extract the `suggestion` field from the JSON. If present, the comment body should include the suggestion markdown block.

### Phase 2: Update poster and format

- [ ] **update-poster**: Update `post_review()` to format the body with the suggestion block when a suggestion is present. The body becomes:
  ```
  **[ERROR]** Function missing type hints

  ```suggestion:-0+0
  def add(a: int, b: int) -> int:
  ```
  ```

- [ ] **update-research-doc**: Add suggestion syntax to `docs/research/gitlab-api.md`.

### Phase 3: Tests

- [ ] **test-parser-suggestions**: Add tests for parsing comments with suggestions (single-line, multi-line, missing suggestion).

- [ ] **test-poster-suggestions**: Add tests for posting comments with suggestion blocks. Verify the body format is correct.

- [ ] **test-e2e-suggestion**: Run a live review and verify suggestions appear as apply-able in the GitLab MR UI.

## Design Decisions

1. **Suggestion in JSON, not raw markdown**: The agent outputs the replacement code in a `suggestion` field in the JSON array. The poster formats it into GitLab's markdown syntax. This keeps parsing clean — we don't need to extract nested code fences from agent output.

2. **Line offsets**: For single-line suggestions, always use `-0+0` (replace the commented line). For multi-line, the agent can specify `suggestion_start_offset` (lines above) and `suggestion_end_offset` (lines below). Defaults to `0, 0` if omitted.

3. **Optional suggestions**: Not every comment needs a suggestion. Only include when the fix is concrete and unambiguous. The `suggestion` field is optional in the JSON schema.

## Dependencies

Phase 1 → Phase 2 → Phase 3 (strict ordering)

---

# (Previous plan: Test Quality — completed)


## Phase 1: Update instructions, agents, and skills

Update the rules so Copilot (and developers) produce better tests going forward.

- [x] **update-python-instructions**: Add test discipline section to `.github/instructions/python.instructions.md`:
  - Coverage: `--cov-fail-under=90` required, `--cov-report=term-missing`
  - Constants: test data as module-level constants or `conftest.py` fixtures, never inline magic strings
  - Fixtures: shared setup in `conftest.py`, factory functions for test data with overridable defaults
  - Layers: unit (mock at boundary), integration (wire internals, mock externals), e2e (real services)
  - Pattern: show a concrete `conftest.py` example

- [x] **update-developer-agent**: Strengthen testing rules in `.github/agents/developer.agent.md`:
  - Definition of Done: add `--cov-fail-under=90` enforcement
  - Testing Rules: add "no magic strings", "shared fixtures in conftest.py", "constants for repeated test data"

- [x] **create-test-quality-skill**: Create `.github/skills/test-quality/SKILL.md`:
  - When to invoke: writing or reviewing tests
  - Concrete Python patterns: conftest.py with factories, parametrized tests, coverage config
  - Anti-patterns: duplicated setup, magic strings, testing library internals, inline payload construction

## Phase 2: Apply instructions to fix tests

Use the updated rules to fix the existing test suite.

- [x] **shared-test-fixtures**: Create `tests/conftest.py` with shared constants and fixtures:
  - Constants: `GITLAB_URL`, `GITLAB_TOKEN`, `WEBHOOK_SECRET`, `HEADERS`, `MR_PAYLOAD`
  - Fixtures: `env_vars`, `client` (ASGITransport)
  - Factories: `make_settings(**overrides)`, `make_mr_payload(**overrides)`

- [x] **dedup-tests**: Refactor `test_webhook.py`, `test_integration.py`, `test_config.py` to use shared fixtures. Remove all duplicated env var setup, payload dicts, magic strings.

- [x] **coverage-review-engine**: Add `review_engine.run_review()` tests. Mock `CopilotClient`, verify session config includes skills/agents/instructions from repo config. Test timeout and empty messages.

- [x] **coverage-remaining**: Cover remaining low-coverage lines:
  - `comment_poster.py:46-48` — inline fallback path
  - `main.py:17-21` — lifespan startup/shutdown
  - `orchestrator.py:57-59` — exception handling
  - `repo_config.py:37-38,53,58` — edge cases

- [x] **enforce-coverage**: Add `--cov-fail-under=90` to `pyproject.toml` pytest config. Verify full suite passes.

## Phase 3: Validate and contribute back

- [x] **validate-instructions**: Run a Copilot review session with the updated instructions against a repo with test issues. Confirm the review output flags magic strings, missing coverage, duplicated fixtures.

- [x] **commit-to-bootstrap**: Copy validated files to `../copilot-bootstrap` and commit:
  - `.github/instructions/python.instructions.md`
  - `.github/agents/developer.agent.md`
  - `.github/skills/test-quality/SKILL.md`

## Dependencies

Phase 1 → Phase 2 → Phase 3 (strict ordering)

Within Phase 2:
- `dedup-tests` depends on `shared-test-fixtures`
- `enforce-coverage` depends on `coverage-review-engine` and `coverage-remaining`

## Duplication inventory (Phase 2 reference)

| What | Files | Lines |
|---|---|---|
| Env var setup | `test_config:10-12`, `test_webhook:36-39`, `test_integration:47-49` |
| Webhook secret constant | `test_webhook:11`, `test_integration:13` |
| Headers constant | `test_webhook:12`, `test_integration:14` |
| MR payload dict | `test_webhook:14-32`, `test_integration:16-34` |
| Client fixture | `test_webhook:42-47`, `test_integration:52-57` |
| `"https://gitlab.example.com"` | `test_config:10`, `test_webhook:37`, `test_integration:47` |
| `"test-token"` | `test_webhook:38`, `test_integration:48` |

## Coverage baseline (Phase 2 reference)

```
review_engine.py     29%   (lines 75-145: SDK interaction)
main.py              74%   (lines 17-21: lifespan)
comment_poster.py    83%   (lines 46-48: fallback)
orchestrator.py      90%   (lines 57-59: error handling)
repo_config.py       95%   (lines 37-38, 53, 58)
TOTAL                84%
```

## Clarification

Phase 2 applies rules across ALL existing test files (not just the duplicated ones). Every test file gets reviewed against the new instructions and fixed. All tests must pass after changes.
