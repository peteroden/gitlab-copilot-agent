# 0001. MR Review Service Architecture

## Status

Accepted

## Context

We're building a GitLab webhook service that uses GitHub Copilot to perform automated merge request reviews. The service must handle async LLM interactions, clone repositories for context, post structured review comments, and support flexible auth models. Key constraints: GitLab webhook 10s timeout, reviews take 30-120+ seconds, need file system access for Copilot agent tools.

## Decision

We choose the following architectural approach across seven key areas:

### 1. Web Framework: FastAPI + Async

**Selected:** FastAPI with native async/await

**Alternatives considered:**
- Flask: synchronous by default, async support bolted on
- Django: heavier, overkill for a webhook service

**Rationale:** Copilot SDK is async-first. FastAPI provides native async support, automatic OpenAPI docs, and Pydantic integration for request validation without ceremony.

### 2. Repository Context Strategy: Clone to Temp Directory

**Selected:** Clone full repo to temporary filesystem location

**Alternatives considered:**
- API-only file fetching: fetch changed files via GitLab API
- Pass diff in prompt: fetch diff via API, embed in user message

**Rationale:** The Copilot SDK provides built-in file and shell tools when given a `working_directory`. Cloning the repo lets the agent use `git diff target...source` to see changes and browse any file for context — no custom tools needed. Cleanup on task completion.

### 3. AI Integration: Copilot SDK Built-in Tools

**Selected:** `github-copilot-sdk` with built-in tools via `working_directory`

**Alternatives considered:**
- Custom tools (`read_file`, `list_directory`, `get_mr_diff`): redundant — the SDK already provides these
- Raw OpenAI/Azure API calls: requires reimplementing agent runtime

**Rationale:** The SDK agent already has file reading, directory listing, and shell execution tools built in. Setting `working_directory` to the cloned repo is sufficient. The agent runs `git diff` itself to see changes. Zero custom tools needed — we provide branch names in the prompt and let the agent work.

### 4. Webhook Processing: Background Task

**Selected:** Return 200 immediately, process review asynchronously

**Alternatives considered:**
- Synchronous webhook response: wait for review completion

**Rationale:** Reviews take 30-120+ seconds. GitLab webhook timeout is 10s. Background task prevents timeout, provides better UX (GitLab shows webhook succeeded), enables retries on failure.

### 5. Comment Strategy: Inline + Summary

**Selected:** Post inline discussion threads on specific lines + summary note

**Alternatives considered:**
- Summary-only comment at MR level

**Rationale:** Inline comments point to exact lines (file path, line number, comment text), summary provides overview and severity rollup. Matches human reviewer workflow. More actionable for developers.

### 6. Auth Model: Configurable (Copilot or BYOK)

**Selected:** Support GitHub Copilot subscription OR Azure OpenAI via environment variables

**Alternatives considered:**
- GitHub Copilot only
- Azure OpenAI only

**Rationale:** Teams have different preferences. Copilot licenses for those with GitHub subscriptions. Azure BYOK for teams wanting cost control, specific models, or data residency. Toggle via config.

### 7. Agent Output Format: Structured JSON with Fallback

**Selected:** Request JSON output (`{file, line, comment, severity}[]`), fallback to free-text parsing

**Alternatives considered:**
- Free-text only: parse unstructured LLM output
- JSON only: fail if agent doesn't comply

**Rationale:** JSON enables precise inline placement. Free-text fallback ensures review is still posted if structured parsing fails (graceful degradation). System prompt requests JSON, but we don't die if LLM hallucinates format.

## Consequences

**Positive:**
- Async architecture handles concurrent webhook processing efficiently
- Built-in SDK tools eliminate custom tool maintenance — agent browses repo naturally
- Background processing prevents webhook timeouts
- Structured output + fallback balances precision with robustness
- Flexible auth supports diverse team requirements

**Negative:**
- Cloning repos adds latency (mitigated by temp dir cleanup)
- Background processing means no immediate feedback to webhook caller (acceptable trade-off)
- Two auth paths increase configuration surface area (documented in README)

## Implementation Dependencies

- `fastapi`, `uvicorn` — web framework
- `python-gitlab` — GitLab API client
- `github-copilot-sdk` — agent runtime
- `pydantic`, `pydantic-settings` — validation, config
- `structlog` — structured logging

## Architecture Flow

```
GitLab Webhook → FastAPI /webhook → Validate token → Extract MR metadata → Background task:
  Clone repo → Create Copilot session (system prompt + working_directory) → Agent diffs + reviews → Parse output → Post inline + summary comments → Cleanup
```
