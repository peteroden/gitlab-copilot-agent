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
      - System prompt (built-in + repo instructions)
      - working_directory → cloned repo (SDK provides built-in file/shell tools)
      - Repo-level skills, agents, instructions from .github/
  → Agent performs review (uses git diff, reads files via built-in tools)
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

### PR 1: Project scaffold + FastAPI skeleton _(Developer)_ ✅
- [x] `pyproject.toml` with pinned dependencies
- [x] `src/gitlab_copilot_agent/` package structure
- [x] `main.py` — FastAPI app with health check endpoint
- [x] `config.py` — Settings via pydantic-settings
- [x] `.devcontainer/devcontainer.json` with copilot CLI
- [x] Tests: health check endpoint, config loading

### PR 2: Webhook endpoint + GitLab event parsing _(Developer)_ ✅
- [x] `webhook.py` — POST /webhook with `X-Gitlab-Token` validation
- [x] `models.py` — Pydantic models for webhook payload
- [x] Returns 200 immediately, queues review as background task
- [x] Tests: webhook validation, payload parsing, rejection of non-MR events

### PR 3: GitLab client — repo cloning + diff fetching _(Developer)_ ✅
- [x] `gitlab_client.py` — clone, fetch MR details, cleanup
- [x] Tests: mock GitLab API responses, verify clone commands

### PR 4: Copilot review engine _(Developer)_ ✅
- [x] `review_engine.py` — CopilotClient with configurable provider
- [x] **Simplified**: No custom tools needed — SDK provides built-in file/shell tools via `working_directory`. Agent runs `git diff target...source` itself.
- [x] Waits for `session.idle` event (not `assistant.message`) to capture final response
- [x] Tests: mock Copilot SDK, verify prompt construction

### PR 5: Review output parsing + GitLab comment posting _(Developer)_ ✅
- [x] `comment_parser.py` — parses agent output into structured comments (file, line, severity)
- [x] `comment_poster.py` — inline discussions + summary note, with fallback for out-of-diff lines
- [x] Tests: output parsing, comment posting with mocked GitLab API

### PR 6: End-to-end integration + deployment _(Developer)_ ✅
- [x] `orchestrator.py` — wires webhook → clone → review → parse → post → cleanup
- [x] `Dockerfile` for deployment
- [x] Integration test with full flow (mocked externals)
- [ ] Update `README.md` with deployment instructions and environment variable reference

### Repo config loading _(Developer)_ ✅
- [x] `repo_config.py` — discovers `.github/`/`.gitlab/` skills, agents, instructions
- [x] Skills loaded via SDK-native `skill_directories` field
- [x] Agents loaded via SDK-native `custom_agents` field (YAML frontmatter parsing)
- [x] Instructions appended to system message
- [x] 12 unit tests
- [x] Live-validated: skills, agents, instructions all loaded during real review

### Test quality improvement ✅
- [x] Updated `developer.agent.md` with language-agnostic test rules
- [x] Created `python.instructions.md` with coverage/conftest patterns
- [x] Created `test-quality` skill with patterns, anti-patterns, checklist
- [x] Created `tests/conftest.py` with shared constants, fixtures, factories
- [x] Refactored all test files to use shared fixtures (eliminated magic strings)
- [x] Coverage: 84% → 95% (43 tests, `--cov-fail-under=90` enforced)
- [x] Contributed instructions back to `../copilot-bootstrap`

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

1. **Clone vs API fetch**: Clone to temp dir. The Copilot agent gets real file system access via SDK built-in tools (no custom tools needed), enabling it to browse the repo and run `git diff` naturally.
2. **No custom tools**: The Copilot SDK provides built-in file read, directory list, and shell execution tools when `working_directory` is set. This eliminated the need for custom `read_file`, `list_directory`, `get_mr_diff` tools.
3. **Structured output**: Agent returns review in a defined format (inline comments with `[ERROR]`/`[WARNING]`/`[INFO]` severity tags + file:line references), parsed into GitLab API calls. Fallback: post as a single summary comment.
4. **Background processing**: Webhook returns 200 immediately; review runs as a FastAPI background task. Long reviews won't timeout the webhook.
5. **Auth flexibility**: Config supports both GitHub Copilot subscription auth and Azure OpenAI BYOK via environment variables.
6. **Repo config loading**: Skills and agents are loaded via SDK-native `SessionConfig` fields (`skill_directories`, `custom_agents`). Instructions are appended to the system message.
7. **session.idle over assistant.message**: Must wait for `session.idle` event (agent finished all tool calls), not `assistant.message` (fires on each intermediate response).

## Remaining Work

- [x] Update `README.md` with deployment instructions and environment variable reference
- [x] Fix structlog `exc_info` rendering (exception tracebacks currently swallowed)
- [ ] Test Azure App Service deployment

---

## Feature: Inline Code Suggestions

### Problem

The agent posts review comments like `**[ERROR]** Function missing type hints` but doesn't propose the actual fix. GitLab supports **suggestions** — a markdown syntax in comment bodies that renders as an apply-able diff. Users click "Apply suggestion" and GitLab commits the fix directly to the MR branch.

### How GitLab Suggestions Work

No special API — suggestions are markdown in the discussion body:

````
```suggestion:-0+0
def add(a: int, b: int) -> int:
```
````

- Uses the same `POST /discussions` endpoint we already call
- `-0+0` = replace just the commented line (0 lines above, 0 below)
- `-2+1` = replace from 2 lines above to 1 line below the commented line
- Max 100 lines above + 100 below (201 total per suggestion)
- Users see "Apply suggestion" button in the GitLab UI
- Multiple suggestions can be batched and applied as a single commit

### Approach

Three areas to change:

1. **Agent prompt** (`review_engine.py`) — instruct the agent to include replacement code in its JSON output
2. **Comment parser** (`comment_parser.py`) — extract the `suggestion` field from the JSON
3. **Comment poster** (`comment_poster.py`) — format the suggestion into GitLab's markdown syntax

### Phase 1: Prompt and parser

- [x] **update-system-prompt**: Update `SYSTEM_PROMPT` in `review_engine.py`:
  - Tell the agent to include a `suggestion` field with the replacement code when the fix is concrete
  - Include `suggestion_start_offset` (lines above) and `suggestion_end_offset` (lines below) for multi-line replacements
  - Only suggest when the fix is unambiguous — not every comment needs a suggestion

- [x] **update-comment-model**: Add fields to `ReviewComment` dataclass in `comment_parser.py`:
  - `suggestion: str | None` — the replacement code (optional)
  - `suggestion_start_offset: int` — lines above the commented line to replace (default 0)
  - `suggestion_end_offset: int` — lines below the commented line to replace (default 0)

- [x] **update-parser**: Update `parse_review()` to extract `suggestion`, `suggestion_start_offset`, `suggestion_end_offset` from the JSON array items

### Phase 2: Poster and docs

- [x] **update-poster**: Update `post_review()` in `comment_poster.py`:
  - When a comment has a `suggestion`, append the suggestion markdown block to the body:
    ```
    **[ERROR]** Function missing type hints

    ```suggestion:-0+0
    def add(a: int, b: int) -> int:
    ```
    ```
  - Use the offset values for multi-line suggestions: `suggestion:-{start}+{end}`

- [x] **update-research-doc**: Add suggestion syntax to `docs/research/gitlab-api.md`

### Phase 3: Tests

- [x] **test-parser-suggestions**: Tests for parsing comments with suggestions (single-line, multi-line offsets, missing suggestion field)
- [x] **test-poster-suggestions**: Tests for posting comments with suggestion blocks (verify body format)
- [x] **test-e2e-suggestion**: Live review against calculator-test repo, verify suggestions appear as apply-able in GitLab MR UI

### Design Decisions

1. **Suggestion in JSON, not raw markdown**: The agent outputs replacement code in a `suggestion` field. The poster formats it into GitLab's markdown. Avoids nested code-fence parsing.
2. **Optional suggestions**: Not every comment needs a suggestion. The field is omitted when the fix isn't concrete.
3. **Offsets default to 0,0**: Single-line replacement (the commented line) unless the agent specifies otherwise.

---

## Feature: Support `.claude/` config root and universal instruction files

### Problem

The repo config discovery currently searches `.github/` and `.gitlab/`. Two issues:

1. `.gitlab/` is for CI/CD configs and MR templates — **not** agent config. GitLab uses `AGENTS.md` (via GitLab Duo) for AI agent instructions. Remove `.gitlab/` as a config root.
2. `.claude/` (Claude Code's project config) is not searched. Add it.
3. `AGENTS.md` is the emerging universal standard for AI agent instructions, supported by Copilot, Claude Code, Codex, Cursor, GitLab Duo, and others. It supports **hierarchical subdirectory loading** — the closest `AGENTS.md` to the file being worked on takes precedence.

### Instruction file landscape

| File | Location | Used by |
|---|---|---|
| `.github/copilot-instructions.md` | `.github/` | GitHub Copilot |
| `.github/instructions/*.md` | `.github/instructions/` | GitHub Copilot (per-language) |
| `AGENTS.md` | Project root + any subdirectory | Universal (Copilot, Claude, Codex, Cursor, GitLab Duo) |
| `CLAUDE.md` | Project root | Claude Code |
| `.claude/CLAUDE.md` | `.claude/` | Claude Code (project-scoped) |

`AGENTS.md` hierarchy: the closest file to the working directory wins. In a monorepo:
```
/AGENTS.md                          ← global conventions
/packages/frontend/AGENTS.md        ← frontend-specific rules
/packages/backend/AGENTS.md         ← backend-specific rules
```

### Changes

**Config roots** — replace `.gitlab/` with `.claude/`:
- [ ] **update-config-roots** _(Developer)_: Change `_CONFIG_ROOTS` from `[".github", ".gitlab"]` to `[".github", ".claude"]`

**Instruction file discovery** — add root-level universal files:
- [ ] **root-instructions** _(Developer)_: After loading config-root-scoped instructions, also discover from the project root:
  1. `AGENTS.md` — walk from root, collect all `AGENTS.md` files (root + subdirectories), concatenate with root first
  2. `CLAUDE.md` — project root only
  
  Load order (all concatenated into system message):
  1. `.github/copilot-instructions.md` + `.github/instructions/*.md`
  2. `.claude/CLAUDE.md` (via config root discovery, treated as instructions file)
  3. `AGENTS.md` (root, then subdirectories)
  4. `CLAUDE.md` (project root)
  
  Deduplicate if files are symlinks to each other (common practice: `ln -s AGENTS.md CLAUDE.md`).

- [ ] **test-config-roots** _(Developer)_: Add tests for `.claude/` discovery, `AGENTS.md` (root + subdirs), `CLAUDE.md`, symlink dedup, and removal of `.gitlab/`
- [ ] **update-docs-config** _(Developer)_: Update README to list all supported instruction file locations

---

## Refactor: Replace regex frontmatter parsing with python-frontmatter

### Problem

`repo_config.py` uses a hand-rolled regex + manual key-value parser for `.agent.md` YAML frontmatter. This is fragile — it doesn't handle multiline values, nested YAML, quoted colons, or TOML/JSON frontmatter. It also silently drops `CustomAgentConfig` fields like `display_name`, `mcp_servers`, and `infer`.

### Changes

- [ ] **add-frontmatter-dep** _(Developer)_: Add `python-frontmatter` to `pyproject.toml` (it pulls in PyYAML)
- [ ] **refactor-parser** _(Developer)_: Replace `_parse_agent_file()` regex with `frontmatter.loads()`:
  ```python
  import frontmatter
  post = frontmatter.loads(text)
  meta = post.metadata  # dict with all YAML fields
  body = post.content    # markdown body
  ```
  Pass through all recognized `CustomAgentConfig` fields: `name`, `description`, `tools`, `display_name`, `mcp_servers`, `infer`
- [ ] **remove-regex** _(Developer)_: Remove `_FRONTMATTER_RE` and the manual parsing loop
- [ ] **test-frontmatter** _(Developer)_: Verify existing tests still pass, add test for nested/complex YAML frontmatter

### Execution Sequence

```
Developer: update-config-roots + add-frontmatter-dep (independent, can parallelize)
    ↓
Developer: root-instructions + refactor-parser + remove-regex (depend on above)
    ↓
Developer: test-config-roots + test-frontmatter (after implementation)
    ↓
Developer: update-docs-config (after tests pass)
```

Branch: `feature/config-roots-and-frontmatter` off `main`

---

## Assumptions

1. The Copilot CLI binary will be available in the Azure App Service container (installed in Dockerfile).
2. The GitLab instance is accessible from Azure (network connectivity).
3. The GitLab token has API access to read repos, read MRs, and post comments/discussions.
4. The `.github/` folder in this project contains the governance/instructions that the agent should follow for reviews.
5. Shallow clone is sufficient for review context (no need for full git history).
