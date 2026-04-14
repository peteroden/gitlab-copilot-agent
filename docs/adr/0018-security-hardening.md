# 0018. Security Hardening (Phase 8)

## Status

Accepted

## Context

The service processes untrusted input from GitLab webhooks, MR metadata, Jira issues, and LLM output. Prior to Phase 8, most untrusted fields were inserted raw into LLM prompts, all FastAPI routes were publicly exposed, the webhook secret doubled as admin auth, coding tasks auto-pushed ready MRs, and no fuzzing infrastructure existed.

Phase 8 addresses these gaps across eight decision areas.

## Decisions

### 1. Prompt Injection: SECURITY_INSTRUCTIONS + Sanitization

**Decision**: Append a non-overridable `SECURITY_INSTRUCTIONS` constant to every LLM prompt (after all user overrides), apply `strip_dangerous_chars()` (NUL/ESC/bidi) and `truncate_untrusted()` (per-field limits) to all untrusted fields, and label untrusted content in prompts.

**Alternative considered**: XML isolation tags (`<untrusted>...</untrusted>`) to structurally separate instructions from data. Rejected because LLMs do not reliably respect XML boundaries as security boundaries — the approach provides false confidence. Defense-in-depth via sanitization + structural separation (file-based prompts) + non-overridable instructions is more robust.

**Consequences**: All prompts grow by ~200 chars. User prompt overrides cannot remove security instructions. Bidi override attacks are neutralized. Per-field truncation prevents prompt budget exhaustion from adversarial field inflation.

### 2. File-Based Prompt Strategy as Default

**Decision**: Default `prompt_strategy` to `"file-based"`. Offload MR context to `.copilot-review/` files and use native `git diff <base_sha> HEAD` instead of inlining diffs. Prompts shrink to <2K chars. Inline mode preserved as `"inline"` fallback.

**Alternative considered**: Inline with total prompt budget (cap all fields to fit a global token limit). Rejected because it still mixes trusted instructions with untrusted data in the same text stream, and prompt budgeting across variable-length fields is fragile.

**Consequences**: Structural separation of instructions from untrusted data. Prompts are smaller and more predictable. Requires the LLM agent to read files and run git commands (supported by Copilot SDK). Inline fallback ensures backward compatibility.

### 3. Shallow Merge-Base Fetch

**Decision**: Run `git fetch --depth=1 origin <base_sha>` in the cloned repo to make `git diff <base_sha> HEAD` work on `--depth=1` shallow clones. The `base_sha` comes from `MRDiffRef.base_sha` in the GitLab API response.

**Alternative considered**: (a) Deepen clone (`git fetch --deepen=N`) — unpredictable N, potentially fetches entire history. (b) Fetch diff via GitLab API and write to file — adds API call, loses git tooling context, file size unpredictable. Both rejected for complexity and reliability.

**Consequences**: One additional `git fetch` per task (~100ms). If fetch fails (e.g., force-pushed base), the pipeline logs a warning and falls back to inline mode. No full clone needed.

### 4. Ingress via FastAPI Middleware

**Decision**: Implement path restriction, IP allowlist, and body size limit as FastAPI HTTP middleware. Path middleware returns 404 for non-allowed paths. IP allowlist uses CIDR matching with proxy-aware `_get_client_ip()` (RFC 7239 rightmost-non-trusted-proxy). Body size middleware wraps the ASGI receive callable for streaming byte counting (handles chunked encoding). FastAPI docs disabled in production.

**Alternative considered**: (a) Network-level restriction via ACA/API Management — not all deployments use ACA; adds infrastructure dependency. (b) Nginx sidecar — additional container, config drift risk, doesn't cover body size for chunked encoding at the application layer. Both rejected in favor of portable application-level middleware that works across all deployment targets.

**Consequences**: All protection is application-level and portable. No infrastructure dependency. IP allowlist is opt-in (empty = allow all). Body size limit prevents OOM from chunked encoding bypass.

### 5. Draft MR Instead of Push Gate

**Decision**: When `auto_merge_enabled=False` (default), the coding pipeline creates Draft MRs by prepending `Draft: ` to the MR title. Jira comment explains manual un-drafting is required.

**Alternative considered**: (a) Hard push gate — block push until human approves. Rejected because trapping code in the container creates state management complexity and the code is already visible in the MR diff. (b) Full approval workflow with GitLab approvals API — deferred as it requires per-project approval rule configuration.

**Consequences**: All LLM-generated code lands in Draft MRs by default. Human must explicitly un-draft before merge. `auto_merge_enabled=True` restores pre-hardening behavior. Partial mitigation — a compromised LLM's code is still pushed to a branch, but cannot be merged without human action.

### 6. Two-Tier Fuzzing: Hypothesis + Atheris

**Decision**: Hypothesis property tests run in `pytest` (CI, every commit). Atheris coverage-guided harnesses run as merge gate on PRs to main (30s budget per harness, fail-open on timeout).

**Alternative considered**: (a) Hypothesis only — good for property invariants but misses coverage-guided edge cases in parsing code. (b) Schemathesis for API-level fuzzing — overkill for internal webhook API already covered by Pydantic strict mode; Atheris targets lower-level parsers (JSON, sanitizer).

**Consequences**: Two complementary fuzzing layers. Hypothesis is fast and deterministic (good for CI). Atheris finds deeper bugs but requires clang/libFuzzer (isolated to merge gate). Corpus accumulates over time for incremental fuzzing.

### 7. No Output Validator

**Decision**: Do not implement a regex-based output validator for LLM responses.

**Rationale**: The task runner has no network or API access beyond Copilot. The controller has no auto-merge endpoint (Draft MR gate). Regex pattern matching on LLM output is security theater — it cannot distinguish malicious intent from legitimate code that happens to match patterns. The real mitigation is pre-merge static analysis in CI (deferred as WI-07).

**Alternative considered**: Regex scanning for `eval()`, `exec()`, credential patterns, etc. Rejected because false positive rate would be unacceptable (these patterns appear in legitimate code), and false negatives are trivial (obfuscation defeats regex).

**Consequences**: No output filtering overhead. Security relies on: (1) Draft MR gate, (2) human review, (3) future static analysis gate (WI-07).

### 8. Separate Admin Auth for `/config/reload`

**Decision**: Add `ADMIN_TOKEN` configuration field. When set, `/config/reload` requires `X-Admin-Token` header (constant-time comparison). When unset, falls back to `X-Gitlab-Token` (webhook secret) for backward compatibility.

**Alternative considered**: Reuse webhook secret for all endpoints. Rejected because a leaked webhook secret (e.g., via GitLab admin panel exposure) would grant config mutation capability, expanding blast radius unnecessarily.

**Consequences**: Config reload is auth-separated from webhook ingestion. Combined with 10s rate limiting per client IP, limits blast radius of credential leaks. Operators who don't set `ADMIN_TOKEN` get the legacy behavior.

## Consequences

- Defense-in-depth across prompt injection, ingress, auth, and code push
- All hardening is application-level and deployment-target agnostic
- File-based prompt strategy improves both security (structural separation) and performance (smaller prompts)
- Draft MR gate is a partial mitigation — full protection requires human review discipline and future static analysis (WI-07)
- Fuzzing infrastructure catches edge cases in parsers and sanitizers that unit tests miss
- No breaking changes — all new features have backward-compatible defaults or opt-in configuration
