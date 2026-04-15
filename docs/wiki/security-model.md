# Security Model

Trust boundaries, authentication mechanisms, input validation, sandbox isolation, and secret handling.

---

## Trust Boundaries

```mermaid
graph TB
    subgraph "Untrusted Zone (Red)"
        GL[GitLab Instance]
        JIRA[Jira Cloud]
        REPO[Git Repository Contents]
        COPAPI[Copilot API / BYOK]
    end
    
    subgraph "Trust Boundary 1: Ingress Middleware"
        PATH_MW[Path Restriction<br/>404 non-allowed]
        BODY_MW[Body Size Limit<br/>10 MB streaming]
        IP_MW[IP Allowlist<br/>CIDR + X-Forwarded-For]
        WH[gitlab_webhook.py<br/>X-Gitlab-Token check]
    end
    
    subgraph "Trusted Zone (Green)"
        APP[Application Code]
        SETTINGS[Environment Config]
        MEMORY[In-Memory State]
        SANITIZER[prompt_sanitizer.py<br/>strip + truncate]
        SEC_INST[SECURITY_INSTRUCTIONS<br/>non-overridable append]
    end
    
    subgraph "Semi-Trusted Zone (Yellow)"
        REDIS[(Redis)]
        K8SJOB[K8s Job Pods]
    end
    
    GL -->|Webhook POST| PATH_MW
    PATH_MW --> BODY_MW
    BODY_MW --> IP_MW
    IP_MW --> WH
    WH -->|Pydantic validation| APP
    GL -.->|API responses| APP
    JIRA -.->|API responses| APP
    REPO -.->|Clone via git| APP
    COPAPI -.->|SDK output| APP
    
    APP --> SANITIZER
    SANITIZER --> SEC_INST
    APP --> REDIS
    APP --> K8SJOB
    K8SJOB --> REDIS
    
    classDef untrusted fill:#ffcccc,stroke:#ff0000
    classDef trusted fill:#ccffcc,stroke:#00aa00
    classDef semi fill:#ffffcc,stroke:#aaaa00
    classDef boundary fill:#cce5ff,stroke:#0066cc
    class GL,JIRA,REPO,COPAPI untrusted
    class APP,SETTINGS,MEMORY,SANITIZER,SEC_INST trusted
    class REDIS,K8SJOB semi
    class PATH_MW,BODY_MW,IP_MW,WH boundary
```

### Untrusted Input (Red Zone)

**Sources**:
1. **GitLab webhook payloads**: POST to `/webhook` from external GitLab instance
2. **GitLab API responses**: MR details, diffs, notes, project metadata
3. **Jira API responses**: Issue data, descriptions (may contain arbitrary text/ADF)
4. **Git repository contents**: Cloned code, `.gitignore`, repo config files (`.github/`, `.claude/`)
5. **Copilot SDK output**: LLM-generated review comments and code changes

**Attack Vectors**:
- Webhook replay without valid HMAC → denied at validation
- Malicious repo URL (e.g., `file://`, `git://`, embedded creds) → rejected by `_validate_clone_url()`
- Symlink attacks in repo config → prevented by `_resolve_real_path()` boundary check
- Malicious git refs (e.g., `../../../etc/passwd`) → git CLI sanitizes
- JSON injection in Jira description → Pydantic strict parsing
- Code injection in SDK output → treated as data, posted as GitLab comment

---

## Authentication

### 1. Webhook HMAC (GitLab → Service)

**Mechanism**: HMAC-SHA256 using shared secret.

**Flow**:
1. GitLab computes HMAC of request body with `GITLAB_WEBHOOK_SECRET`
2. Sends as `X-Gitlab-Token` header
3. `gitlab_webhook.py` recomputes HMAC and compares using `hmac.compare_digest()` (constant-time)
4. Invalid token → 401 Unauthorized

**Code**: `gitlab_webhook.py` → `_validate_webhook_token()`

**Threat**: If secret is compromised, attacker can replay webhooks or trigger arbitrary reviews.

**Mitigation**: Rotate `GITLAB_WEBHOOK_SECRET` regularly, use Kubernetes Secret storage.

---

### 2. GitLab API Token (Service → GitLab)

**Mechanism**: Bearer token in `Private-Token` header.

**Scopes Required**:
- `api`: Full API access (read/write repos, MRs, comments)

**Code**: `gitlab_client.py` uses `python-gitlab` library with token.

**Threat**: If `GITLAB_TOKEN` is compromised, attacker can:
- Read all project code
- Modify MRs, post comments
- Create/delete branches
- Impersonate the agent

**Mitigation**:
- Use project access tokens (scoped to specific projects) instead of personal tokens
- Rotate token regularly
- Store in Kubernetes Secret
- Audit GitLab API logs for suspicious activity

**Per-Credential Identity Caching**:
Each GitLab token maps to a different user. The `CredentialRegistry` lazily discovers and caches the agent's identity by calling `GET /user` on first use per credential. The returned `AgentIdentity` (immutable `user_id` + display `username`) is stored in-memory for the lifetime of the process. Identity is used for self-comment detection during review — matching on the immutable `user_id` rather than the mutable `username` prevents bypass via username changes.

---

### 3. GitHub Token (Service → Copilot API)

**Mechanism**: Bearer token for GitHub Copilot API access.

**Scopes Required**:
- `copilot`: Access to Copilot API

**Code**: `copilot_session.py` passes token to SDK via `GITHUB_TOKEN` env var.

**Threat**: If token is compromised, attacker can:
- Consume Copilot API quota
- Generate arbitrary code via SDK

**Mitigation**:
- Use GitHub App tokens (scoped, short-lived) instead of PATs
- Store in Kubernetes Secret
- Exclude from SDK subprocess env (only allowlisted vars passed)

---

### 4. BYOK Provider API Key (Service → LLM Provider)

**Mechanism**: API key for Azure OpenAI, OpenAI, or other provider.

**Code**: `copilot_session.py` passes via `ProviderConfig`.

**Threat**: If compromised, attacker can consume LLM quota.

**Mitigation**: Store in Kubernetes Secret, rotate regularly, use provider's rate limiting.

---

### 5. Jira Basic Auth (Service → Jira)

**Mechanism**: HTTP Basic auth (`email:api_token` base64-encoded).

**Code**: `jira_client.py` sets `Authorization: Basic` header.

**Threat**: If compromised, attacker can:
- Read all issues
- Transition issues
- Add comments

**Mitigation**: Use API token (not password), store in Kubernetes Secret, scope to minimal permissions.

---

### 6. Admin Token (`/config/reload`)

**Mechanism**: Static token in `X-Admin-Token` header, compared with `hmac.compare_digest()`.

**Code**: `main.py` → `config_reload()`. When `admin_token` is configured, `X-Admin-Token` is required. When unset, falls back to `X-Gitlab-Token` (webhook secret).

**Rationale**: Separates config mutation auth from webhook ingestion auth. A leaked webhook secret no longer grants config-reload capability.

**Threat**: If `ADMIN_TOKEN` is compromised, attacker can hot-reload the project registry (DoS or misconfiguration).

**Mitigation**: Separate from webhook secret, constant-time comparison, rate-limited (10s cooldown per IP).

---

## Input Validation

### Webhook Payloads

**Validation Layer 1: HMAC**
- Rejects all requests without valid `X-Gitlab-Token`
- Constant-time comparison prevents timing attacks

**Validation Layer 2: Pydantic Strict Mode**
- All webhook models use `strict=True`
- Rejects unknown fields
- Enforces exact type matching (no coercion)
- Example: `{"iid": "7"}` rejected (must be `int`, not `str`)

**Validation Layer 3: Business Logic**
- Project allowlist: `allowed_project_ids` checked before processing
- Action filter: only `"open"` and `"update"` actions handled
- Commit check: `oldrev` must be present for `"update"` (skips title-only updates)
- Self-comment guard: skip notes authored by `agent_gitlab_username`

**Validation Layer 4: Body Size Enforcement**
- ASGI middleware (`limit_body_size`) enforces 10 MB (`MAX_BODY_SIZE`) limit before JSON parsing
- Fast path: rejects `Content-Length` headers exceeding the limit
- Streaming path: wraps the ASGI receive callable to count bytes for chunked transfer encoding, raises 413 if exceeded
- Prevents OOM from oversized payloads bypassing `Content-Length` via chunked encoding

**Code**: `gitlab_webhook.py`, `models.py`, `main.py`

---

### Clone URLs

**Validation**: `git/validation.py` → `_validate_clone_url()`

**Checks**:
1. Valid URL format (via `urlparse`)
2. Scheme must be `https` (rejects `file://`, `git://`, `ssh://`)
3. No embedded credentials (rejects `https://user:pass@host/repo.git`)
4. Must have valid host and path

**Threat**: Malicious webhook could provide `clone_url` pointing to local filesystem or attacker-controlled server.

**Mitigation**: Strict URL validation before clone, token sanitized in error messages.

---

### Repo Config Files

**Symlink Protection**: `repo_config.py` → `_resolve_real_path()`

**Checks**:
1. Resolves symlinks to real paths
2. Ensures resolved path is within repo root (via `is_relative_to()`)
3. Rejects paths escaping repository boundary
4. Logs and skips rejected paths

**Threat**: Malicious `.github/AGENTS.md` → symlink to `/etc/passwd` → agent reads system file.

**Mitigation**: Boundary check, read-only filesystem in K8s Job pod.

---

### Copilot SDK Output

**Parsing**: `comment_parser.py` → `parse_review()`

**Strategy**: Treat as untrusted data, extract structured JSON, fall back to plain text.

**Checks**:
1. Regex extraction of JSON array (code fence or raw)
2. `json.loads()` with exception handling
3. Validate each comment dict (required keys: `file`, `line`, `comment`)
4. Type coercion for fields (e.g., `int(item["line"])`)
5. Invalid comments skipped (no crash)

**Threat**: LLM generates malicious JSON (e.g., path traversal in `file` field).

**Mitigation**: Posted as GitLab comment (no filesystem access), position validation prevents invalid inline comments.

---

### Coding Patches (K8s Executor)

**Source**: K8s Job pod captures `git diff --cached --binary` after Copilot modifies files, stores as `CodingResult.patch` in Redis.

**Validation** (`coding_workflow.py` → `apply_coding_result()`, `git/patches.py` → `_validate_patch()`):

**Checks**:
1. `base_sha` validation — patch base must match local HEAD (detects clone divergence or replay)
2. Path traversal scan — rejects patches containing `../` in file headers (`diff --git a/`, `--- a/`, `+++ b/`)
3. Size limit — `MAX_PATCH_SIZE` (10 MB) prevents Redis OOM and excessive disk write
4. Applied via `git apply --3way` — git validates patch format and refuses malformed hunks

**Threats**:
- **Patch injection**: Compromised Job pod writes crafted patch to Redis (e.g., overwrite `.github/workflows/` to gain CI execution)
- **Path traversal**: Patch references `../../etc/crontab` to escape repo boundary
- **Replay attack**: Attacker replays old CodingResult from Redis to overwrite newer work
- **Oversized patch**: DoS via large diff consuming Redis memory and controller disk

**Mitigations**:
- Path traversal blocked by `_validate_patch()` before `git apply`
- `git apply` itself rejects paths outside the work tree
- `base_sha` check prevents replay (old base_sha won't match current HEAD)
- Size limit prevents resource exhaustion
- Only the controller has git push access — a malicious patch is visible in the MR diff before merge

---

## Prompt Injection Defenses

### SECURITY_INSTRUCTIONS (Non-Overridable Append)

**Mechanism**: A constant `SECURITY_INSTRUCTIONS` block is unconditionally appended to every LLM prompt by `get_prompt()` in `prompt_defaults.py`. User prompt overrides and suffixes are resolved first; the security instructions are appended last with no opt-out mechanism.

**Content**: Instructs the LLM to treat all MR metadata as untrusted, never follow embedded instructions, never approve/merge MRs, and flag suspicious content as potential prompt injection.

**Code**: `prompt_defaults.py` → `SECURITY_INSTRUCTIONS`, `get_prompt()`

### Untrusted Content Sanitization

**`strip_dangerous_chars()`**: Removes NUL, C0 control characters (except tab/newline/CR), bidi overrides (U+202A–U+202E), and bidi isolates (U+2066–U+2069). Preserves ZWJ (U+200D) and ZWNJ (U+200C) needed by Arabic, Indic, and emoji text.

**`truncate_untrusted()`**: Enforces per-field character limits in inline prompt mode. Limits: MR title 500, MR description 5000, note body 5000, Jira description 5000, commit message 4000. Appends a truncation notice when shortened.

**Code**: `prompt_sanitizer.py`

### Untrusted Labeling in Engines

All three prompt engines (review, discussion, coding) apply both sanitizers to untrusted fields:

| Engine | Fields Sanitized |
|--------|-----------------|
| `review_engine.py` | MR title, MR description |
| `discussion_engine.py` | MR title, MR description, note body, other discussion notes |
| `coding_engine.py` | Jira issue summary, Jira description |

In inline mode, `truncate_untrusted()` + `strip_dangerous_chars()` are applied. In file-based mode, only `strip_dangerous_chars()` is applied (content is in separate files, not inlined in the prompt).

---

## File-Based Prompt Strategy

**Default mode**: `prompt_strategy="file-based"` (configurable in `config/settings.py`).

**Mechanism**: Instead of inlining all MR context in the LLM prompt, the pipeline:

1. Writes context files to a sibling `-context/` directory outside the git worktree (e.g., `/tmp/mr-review-abc123-context/`):
   - `mr-description.md` — MR description
   - `prior-feedback.md` — Unresolved review comments
   - `suppressed-feedback.md` — Resolved/dismissed items
   - `current-thread.md` — Full triggering discussion thread (discussion engine)
   - `other-discussions.md` — Other active threads (discussion engine)
   - `jira-issue.md` — Jira issue details (coding engine)

   Writing to a sibling directory (not inside the repo) prevents two attack vectors:
   - **Symlink traversal**: repository content cannot influence the write path
   - **Accidental git contamination**: `git add .` cannot capture context files since they live outside the worktree
2. Shallow-fetches the merge base: `git fetch --depth=1 origin <base_sha>` to enable native `git diff <base_sha> HEAD` in the cloned repo
3. Generates a minimal prompt (<2K chars) referencing the context files and git commands

**Security benefit**: Structural separation of trusted instructions from untrusted data. The prompt contains only instructions; untrusted content lives in files the LLM reads separately.

**Fallback**: `prompt_strategy="inline"` preserves the original behavior with all content inlined in the prompt.

**Code**: `review_pipeline.py`, `discussion_pipeline.py`, `coding_pipeline.py` (prepare stages), `pipeline.py` → `write_context_file()`

---

## Ingress Restriction

### Path Restriction Middleware

**Mechanism**: `restrict_paths` middleware returns 404 for any path not in `ALLOWED_PATHS = {"/webhook", "/health", "/config/reload"}`.

**Effect**: FastAPI docs (`/docs`, `/openapi.json`) disabled in production via `ENVIRONMENT != "development"` check. All other undefined paths return 404 instead of FastAPI's default behavior.

**Code**: `main.py` → `restrict_paths()`

### Webhook IP Allowlist

**Mechanism**: `ip_allowlist_middleware` rejects `/webhook` requests from IPs outside configured CIDR ranges. Non-webhook paths pass through unconditionally.

**IP extraction**: `_get_client_ip()` uses the rightmost-non-trusted-proxy algorithm (RFC 7239). Trusts `X-Forwarded-For` only when the direct connection comes from a `trusted_proxies` CIDR range, then walks rightmost-to-leftmost to find the first non-trusted IP.

**Config**: `webhook_ip_allowlist` (comma-separated CIDRs), `trusted_proxies` (comma-separated CIDRs). Empty allowlist = allow all.

**Code**: `main.py` → `ip_allowlist_middleware()`, `_get_client_ip()`

### Rate Limiting

**Scope**: `/config/reload` endpoint only.

**Mechanism**: 10-second cooldown per client IP (`_RELOAD_COOLDOWN`). Returns 429 with `Retry-After` header when rate exceeded. Uses `time.monotonic()` for clock-skew resistance.

**Code**: `main.py` → `config_reload()`

---

## Draft MR Gate

**Default**: `auto_merge_enabled=False` in `config/settings.py`.

**Mechanism**: When `auto_merge_enabled=False`, the coding pipeline prepends `Draft: ` to the MR title, creating a Draft MR in GitLab. The Jira comment explains that auto-merge is disabled and the MR requires manual un-drafting.

**Security benefit**: Partial mitigation of prompt injection → malicious code push. Even if a compromised LLM generates malicious code, it lands in a Draft MR that requires human review and explicit un-drafting before merge.

**Opt-in**: Set `auto_merge_enabled=True` to restore pre-hardening behavior (ready MRs).

**Code**: `coding_pipeline.py` → `execute()`, `config/settings.py`

---

## Fuzzing Infrastructure

### Hypothesis Property Tests (CI)

**Purpose**: Fast, deterministic property-based testing within `pytest`.

**Targets**:
- `test_prompt_sanitizer.py` — `strip_dangerous_chars()` never produces dangerous chars, `truncate_untrusted()` respects limits
- `test_ingress.py` — `_get_client_ip()` never crashes on arbitrary input

**Execution**: Runs as part of `uv run pytest` in CI on every commit.

### Atheris Coverage-Guided Harnesses (Merge Gate)

**Purpose**: Coverage-guided fuzzing using Google Atheris (libFuzzer-based) to find edge cases that property tests miss.

**Harnesses** (`fuzz/`):
- `fuzz_webhook_payload.py` — Feeds arbitrary bytes to Pydantic webhook model
- `fuzz_sanitizer.py` — Bridges Hypothesis property tests into Atheris via `fuzz_one_input`

**Execution**: 30-second time budget per harness on PRs to main. Fail-open on timeout.

**Code**: `fuzz/`, `fuzz/README.md`

---

## Sandbox Isolation

### Local Executor

**Process Boundary**: SDK runs as subprocess of main process.

**Isolation**:
- Subprocess has same UID/GID as parent (non-root `app:app`)
- Minimal env vars passed (see `_SDK_ENV_ALLOWLIST`)
- Service secrets (`GITLAB_TOKEN`, `JIRA_API_TOKEN`, `GITLAB_WEBHOOK_SECRET`) excluded from SDK env
- `GITHUB_TOKEN` included (required for SDK)

**Limitations**:
- Subprocess can see parent env via `/proc/{ppid}/environ` (requires same UID)
- Subprocess shares filesystem (can read cloned repos, no chroot)
- No resource limits (can consume CPU/memory until OOM)

**Code**: `copilot_session.py` → `build_sdk_env()`

**Threat**: Malicious SDK output includes prompt injection instructing agent to exfiltrate env vars.

**Mitigation**: SDK output treated as data, not executed.

---

### Kubernetes Executor

**Pod Boundary**: Each task runs in ephemeral Job pod.

**Isolation**:
- Separate pod with own network namespace
- `securityContext`:
  - `runAsNonRoot: true`
  - `runAsUser: 1000`
  - `readOnlyRootFilesystem: true`
  - `capabilities.drop: ["ALL"]`
- Resource limits: CPU, memory
- `HOME=/tmp` (writable tmpfs for Copilot CLI state)
- TTL after finished: 300s (pod auto-deleted)
- Optional `hostAliases` for custom DNS resolution

**Credentials Passed** (via explicit K8s Secret key refs):
- `GITHUB_TOKEN` (or `COPILOT_PROVIDER_API_KEY`) for Copilot/LLM auth
- `AZURE_STORAGE_CONNECTION_STRING` for queue dequeue and blob download

Only the secrets needed by Job pods are mounted. **GITLAB_TOKEN is never passed to the runner** — it receives the repo via blob transfer from the controller. Other secrets (`JIRA_*`, `GITLAB_WEBHOOK_SECRET`) are also excluded.

**Result Path**: Pod stores `CodingResult` (summary + patch + base_sha) or `ReviewResult` (summary only) in Azure Blob Storage. Controller reads result — only the controller commits, pushes, and posts API calls.

**Threat**: Malicious repo code executed during review → can read pod env, exfiltrate tokens.

**Mitigation**: 
- ReadOnlyRootFilesystem prevents writing malicious binaries
- Pod cannot push code (no git push credentials or capability)
- Results validated before apply: `base_sha` match, path traversal scan, size limit
- Agent does not execute arbitrary code (only SDK, git, standard tools)
- No network egress policy yet (attacker can still exfiltrate via DNS/HTTP — see Recommended Hardening)
- Egress restricted by NetworkPolicy when deployed via Helm (allows Copilot API, Azure Storage, DNS — GitLab egress still permitted but no longer needed)

**Code**: `remote_executor.py` → `execute()`

### Azure Container Apps Executor

**Container Boundary**: Each task runs in an ephemeral Container Apps Job execution.

**Isolation**:
- Separate container instance per execution
- User-assigned managed identity (no shared service principal)
- Separate identities for controller and job (S4 — least privilege)
- Resource limits: CPU, memory configured per job template
- Execution auto-cleaned by Azure after completion

**Secret Management (S1)**:
- Secrets configured as Key Vault references on the Job template
- **Never passed per-execution** — only non-sensitive env vars (task_id, repo_blob_key, prompts) are overridden at runtime
- Key Vault access scoped via RBAC (Key Vault Secrets User role)

**Identity Separation (S4)**:
- Controller identity: ACR pull, Key Vault read (all secrets), Job trigger
- Job identity: ACR pull, Key Vault read (task secrets only)

**Authentication (S3)**: OIDC federation for CI/CD — no stored Azure credentials in GitHub Actions.

**Result Path**: Job stores result in Azure Blob Storage (via private endpoint). Controller reads result — only the controller posts API calls.

**Code**: `remote_executor.py` → `execute()` (unified for both K8s and ACA backends)

### Plugin Isolation
- Per-session HOME directory (`tempfile.mkdtemp()`) prevents plugin state leakage between sessions/repos
- Plugin install subprocess receives minimal env: `HOME` + `PATH` only
- Service secrets (`GITLAB_TOKEN`, `AZURE_STORAGE_CONNECTION_STRING`, etc.) excluded from plugin process
- Marketplace URLs sanitized before logging (credentials/query params stripped)
- Session HOME cleaned up in `finally` block after session completes
- Plugin install timeout (120s) with subprocess kill to prevent orphaned processes

---

## Secret Handling

### Storage

**Kubernetes Secrets**:
- All tokens stored in Secret resource
- Mounted as env vars via Helm chart
- Encrypted at rest (K8s etcd encryption)
- RBAC-protected (only service account can read)

**Code**: `helm/gitlab-copilot-agent/templates/secret.yaml`

**Values** (all stored in K8s Secret):
- `GITLAB_TOKEN`
- `GITLAB_WEBHOOK_SECRET`
- `GITHUB_TOKEN`
- `COPILOT_PROVIDER_API_KEY`
- `JIRA_API_TOKEN`
- `AZURE_STORAGE_CONNECTION_STRING`

**Access**: The controller pod receives all secrets via `envFrom`. Job pods (task runner) receive only `GITHUB_TOKEN`, `COPILOT_PROVIDER_API_KEY`, and `AZURE_STORAGE_CONNECTION_STRING` via explicit `secretKeyRef` entries — they never see `GITLAB_TOKEN`, `GITLAB_WEBHOOK_SECRET`, or `JIRA_API_TOKEN`.

---

### Exclusion from Logs

**Git Errors**: `git/` package → token replaced with `***` in error messages.

**URL Sanitization**: `_sanitize_url_for_log()` removes credentials from URLs.

**OTEL**: Trace/log processors do not capture env vars.

---

### Exclusion from SDK Subprocess

**Allowlist**: `copilot_session.py` → `_SDK_ENV_ALLOWLIST`

**Included**: `PATH`, `HOME`, `LANG`, `TERM`, `TMPDIR`, `USER`, `GITHUB_TOKEN`

**Excluded**: `GITLAB_TOKEN`, `JIRA_API_TOKEN`, `GITLAB_WEBHOOK_SECRET`, `COPILOT_PROVIDER_API_KEY`

**Rationale**: SDK only needs GitHub token; service secrets provide no value to agent, increase blast radius if SDK compromised.

---

## Network Boundaries

### Inbound Traffic

| Source | Destination | Port | Protocol | Auth | Trust |
|--------|-------------|------|----------|------|-------|
| GitLab | Service `/webhook` | 8000 | HTTPS | HMAC + IP allowlist | Untrusted → Trusted |
| Admin | Service `/config/reload` | 8000 | HTTPS | Admin token + rate limit | Untrusted → Trusted |
| LoadBalancer | Service `/health` | 8000 | HTTP | None | Internal only |
| OTEL Collector | Service (metrics/traces) | N/A | gRPC | None | Internal only |

All inbound traffic passes through path restriction (404 for non-allowed paths), body size limit (10 MB), and IP allowlist middleware before reaching route handlers.

**Firewall**: Kubernetes NetworkPolicies deployed by Helm restrict traffic:
- **Controller pod**: Ingress on port 8000, egress to GitLab/Copilot/Jira APIs, Redis, K8s API, DNS
- **Job pods**: No ingress, egress currently allows HTTPS/HTTP/SSH/git and Azure Storage (NetworkPolicy permits broad outbound — tightening to Copilot API + Azure Storage only is recommended now that GitLab access is no longer needed)
- **Redis pod**: Ingress from controller and job pods only (port 6379), no egress

NetworkPolicies use `app.kubernetes.io/instance` labels to scope to the Helm release, preventing cross-release access.

---

### Outbound Traffic

| Source | Destination | Protocol | Auth | Data |
|--------|-------------|----------|------|------|
| Service | GitLab API | HTTPS | Bearer token | MR metadata, comments |
| Service | Jira API | HTTPS | Basic auth | Issue data, transitions |
| Service | Copilot API / BYOK | HTTPS | Bearer / API key | Code review prompts |
| Service | Azure Storage | HTTPS/HTTP | Connection string / MI | Queue, blobs (params, results, repo tarballs) |
| Service | K8s API | HTTPS | ServiceAccount token | Job creation, status |
| K8s Job | Copilot API | HTTPS | Bearer / API key | Code generation |
| K8s Job | Azure Storage | HTTPS/HTTP | Connection string | Repo blob download, result upload |

**Azure Storage Security**: 
- Production uses Managed Identity (`DefaultAzureCredential`); connection strings used only for local Azurite dev
- Kubernetes NetworkPolicy restricts egress to required endpoints ✅
- Storage account policy enforces `shared_access_key_enabled=false` in production

---

## Attack Surface Summary

| Component | Attack | Impact | Mitigation |
|-----------|--------|--------|------------|
| Webhook endpoint | HMAC bypass | RCE via malicious repo URL | Constant-time HMAC comparison, URL validation |
| Webhook endpoint | Replay attack | Duplicate reviews, resource exhaustion | Deduplication store, idempotency keys |
| Webhook endpoint | Oversized payload | OOM before JSON parsing | ASGI body size middleware (10 MB, streaming) |
| Webhook endpoint | IP spoofing | Bypass allowlist | RFC 7239 rightmost-non-trusted-proxy, trusted proxy validation |
| LLM prompt | Prompt injection via MR fields | Exfiltrate data, manipulate reviews | `SECURITY_INSTRUCTIONS` append, `strip_dangerous_chars()`, `truncate_untrusted()`, file-based prompt separation |
| LLM prompt | Bidi override smuggling | Hide malicious instructions | Bidi override/isolate stripping (U+202A–U+202E, U+2066–U+2069) |
| `/config/reload` | Credential reuse from webhook secret | Config mutation on secret leak | Separate `ADMIN_TOKEN`, constant-time comparison |
| `/config/reload` | Brute force / DoS | Service disruption | 10s rate limit per client IP, 429 with Retry-After |
| Coding pipeline | Prompt injection → malicious code push | Backdoored code in production | Draft MR by default (`auto_merge_enabled=False`), human review required |
| GITLAB_TOKEN | Compromise | Repo write access, impersonation | Project access tokens, rotation, audit logs |
| GITHUB_TOKEN | Compromise | Copilot quota abuse | GitHub App tokens, SDK env isolation |
| JIRA_API_TOKEN | Compromise | Issue manipulation | Scoped permissions, rotation |
| Redis | Unauthorized access | Lock bypass, result tampering | NetworkPolicy ✅, AUTH ✅, consider TLS |
| Redis | CodingResult tampering | Inject malicious patch into commit | base_sha validation, path traversal scan, MR review gate |
| Copilot SDK | Prompt injection | Exfiltrate env vars via output | Env allowlist, output treated as data |
| K8s Job | Malicious repo code | Token exfiltration | ReadOnlyRootFilesystem, no push access, egress NetworkPolicy |
| K8s Job | Malicious patch injection | Write arbitrary files in MR | Path traversal check, MAX_PATCH_SIZE, git apply validation |
| Repo config | Symlink escape | Read system files | Boundary check in `_resolve_real_path()` |
| Clone URL | SSRF / local file access | Read local files, internal services | HTTPS-only, no embedded creds, git CLI validation |

---

## Recommended Hardening

1. **NetworkPolicy**: Restrict Redis to service + job pods only ✅ (implemented — PR #164)
2. **Redis AUTH + TLS**: Enable password authentication and encryption in transit ✅ AUTH (implemented — PR #166), TLS deferred
3. **K8s Secrets for Job pods**: Mount credentials via Secret refs, not env vars in configmap ✅ (implemented — PR #163)
4. **GitLab IP Allowlist**: Restrict `/webhook` endpoint ✅ (implemented — Phase 8, `ip_allowlist_middleware` with CIDR + proxy-aware `_get_client_ip()`)
5. **Project Access Tokens**: Use instead of personal tokens for GITLAB_TOKEN ✅ (supported — any token with `api` scope works; project access tokens are recommended for least-privilege scoping)
6. **GitHub App Tokens**: Use instead of PATs for GITHUB_TOKEN ✅ (supported — the Copilot SDK accepts any valid GitHub token including fine-grained PATs and GitHub App installation tokens)
7. **Audit Logging**: Enable GitLab/Jira API audit logs
8. **Rotate Secrets**: Quarterly rotation of all tokens/keys ✅ (supported — all credentials are env vars; rotate by updating K8s Secret or External Secrets source and restarting pods. Redis password rotation documented in deployment guide)
9. **Egress NetworkPolicy**: Block job pod egress to internal services (allow GitLab, Copilot API, Redis only) ✅ (implemented — PR #164)
10. **Resource Quotas**: Limit job pod resource consumption
11. **Pin Docker Base Images**: Use digest-based pins to prevent supply chain attacks ✅ (implemented — `Dockerfile` uses `@sha256:` pins, CI validates, Dependabot updates)
12. **Review Gate for @mention coding**: Require human approval before auto-push on coding commands ✅ (implemented — Phase 8, Draft MR by default with `auto_merge_enabled=False`)
13. **Output validator / static analysis**: Regex-based output validation is security theater (no execution context). Real mitigation is pre-merge static analysis in CI (deferred — WI-07)

---

## Threat Model Summary

**Threat Actors**:
1. **External Attacker**: Attempts webhook replay without valid HMAC
2. **Compromised GitLab Instance**: Sends malicious webhooks with valid HMAC
3. **Malicious Repository**: Contains symlinks, prompt injection in config files
4. **Compromised LLM**: Copilot SDK generates malicious code/output
5. **Internal Attacker**: Has access to K8s cluster, can read Secrets

**Security Goals**:
1. **Confidentiality**: Secrets not leaked in logs, SDK output, or errors
2. **Integrity**: Reviews/comments not tampered with by unauthorized parties
3. **Availability**: Service remains operational under load/attack
4. **Auditability**: All actions logged with trace context

**Residual Risks**:
1. Redis compromise allows lock bypass, dedup poisoning, and CodingResult tampering (mitigate: AUTH + NetworkPolicy + TLS)
2. K8s Job can exfiltrate tokens via network (mitigate: egress NetworkPolicy)
3. Copilot API compromise can generate malicious reviews or code patches (mitigate: manual MR review before merge, patch validation, Draft MR gate)
4. HMAC secret compromise allows full webhook replay (mitigate: rotation, monitoring)
5. Prompt injection in @mention interaction could instruct agent to write malicious code — the patch lands as a Draft MR, not directly on main (mitigate: Draft MR + human review + deferred static analysis gate)
6. Sophisticated prompt injection may bypass `SECURITY_INSTRUCTIONS` — defense-in-depth via structural separation (file-based prompts), sanitization, and Draft MR gate
