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
    
    subgraph "Trust Boundary 1: HMAC Validation"
        WH[webhook.py<br/>X-Gitlab-Token check]
    end
    
    subgraph "Trusted Zone (Green)"
        APP[Application Code]
        SETTINGS[Environment Config]
        MEMORY[In-Memory State]
    end
    
    subgraph "Semi-Trusted Zone (Yellow)"
        REDIS[(Redis)]
        K8SJOB[K8s Job Pods]
    end
    
    GL -->|Webhook POST| WH
    WH -->|Pydantic validation| APP
    GL -.->|API responses| APP
    JIRA -.->|API responses| APP
    REPO -.->|Clone via git| APP
    COPAPI -.->|SDK output| APP
    
    APP --> REDIS
    APP --> K8SJOB
    K8SJOB --> REDIS
    
    classDef untrusted fill:#ffcccc,stroke:#ff0000
    classDef trusted fill:#ccffcc,stroke:#00aa00
    classDef semi fill:#ffffcc,stroke:#aaaa00
    class GL,JIRA,REPO,COPAPI untrusted
    class APP,SETTINGS,MEMORY trusted
    class REDIS,K8SJOB semi
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
3. `webhook.py` recomputes HMAC and compares using `hmac.compare_digest()` (constant-time)
4. Invalid token → 401 Unauthorized

**Code**: `webhook.py` → `_validate_webhook_token()`

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

**Code**: `webhook.py`, `models.py`

---

### Clone URLs

**Validation**: `git_operations.py` → `_validate_clone_url()`

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

**Validation** (`coding_workflow.py` → `apply_coding_result()`, `git_operations.py` → `_validate_patch()`):

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

**Credentials Passed**:
- `GITLAB_TOKEN` (clone only — pod has no push access)
- `GITHUB_TOKEN` (or `COPILOT_PROVIDER_API_KEY`) as env vars
- `GITLAB_WEBHOOK_SECRET` (required by Settings validation, not used in pod)

**Result Path**: Pod stores `CodingResult` (summary + patch + base_sha) or `ReviewResult` (summary only) in Redis. Controller reads result — only the controller commits, pushes, and posts API calls.

**Threat**: Malicious repo code executed during review → can read pod env, exfiltrate tokens.

**Mitigation**: 
- ReadOnlyRootFilesystem prevents writing malicious binaries
- Pod cannot push code (no git push credentials or capability)
- Results validated before apply: `base_sha` match, path traversal scan, size limit
- Agent does not execute arbitrary code (only SDK, git, standard tools)
- No network egress policy yet (attacker can still exfiltrate via DNS/HTTP — see Recommended Hardening)

**Code**: `k8s_executor.py` → `_create_job()`

---

## Secret Handling

### Storage

**Kubernetes Secrets**:
- All tokens stored in Secret resource
- Mounted as env vars via Helm chart
- Encrypted at rest (K8s etcd encryption)
- RBAC-protected (only service account can read)

**Code**: `helm/gitlab-copilot-agent/templates/secret.yaml`

**Values**:
- `GITLAB_TOKEN`
- `GITLAB_WEBHOOK_SECRET`
- `GITHUB_TOKEN`
- `COPILOT_PROVIDER_API_KEY`
- `JIRA_API_TOKEN`

---

### Exclusion from Logs

**Git Errors**: `git_operations.py` → token replaced with `***` in error messages.

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
| GitLab | Service `/webhook` | 8000 | HTTPS | HMAC | Untrusted → Trusted |
| LoadBalancer | Service `/health` | 8000 | HTTP | None | Internal only |
| OTEL Collector | Service (metrics/traces) | N/A | gRPC | None | Internal only |

**Firewall**: Kubernetes NetworkPolicy should restrict `/webhook` to GitLab IP range.

---

### Outbound Traffic

| Source | Destination | Protocol | Auth | Data |
|--------|-------------|----------|------|------|
| Service | GitLab API | HTTPS | Bearer token | MR metadata, comments |
| Service | Jira API | HTTPS | Basic auth | Issue data, transitions |
| Service | Copilot API / BYOK | HTTPS | Bearer / API key | Code review prompts |
| Service | Redis | Redis (6379) | None | Locks, dedup keys, results |
| Service | K8s API | HTTPS | ServiceAccount token | Job creation, status |
| K8s Job | GitLab API | HTTPS | Bearer token | Repo clone |
| K8s Job | Copilot API | HTTPS | Bearer / API key | Code generation |
| K8s Job | Redis | Redis (6379) | None | Store results |

**Redis Security**: 
- No auth (relies on network isolation)
- Use Kubernetes NetworkPolicy to restrict to service + jobs only
- Consider Redis password + TLS for production

---

## Attack Surface Summary

| Component | Attack | Impact | Mitigation |
|-----------|--------|--------|------------|
| Webhook endpoint | HMAC bypass | RCE via malicious repo URL | Constant-time HMAC comparison, URL validation |
| Webhook endpoint | Replay attack | Duplicate reviews, resource exhaustion | Deduplication store, idempotency keys |
| GITLAB_TOKEN | Compromise | Repo write access, impersonation | Project access tokens, rotation, audit logs |
| GITHUB_TOKEN | Compromise | Copilot quota abuse | GitHub App tokens, SDK env isolation |
| JIRA_API_TOKEN | Compromise | Issue manipulation | Scoped permissions, rotation |
| Redis | Unauthorized access | Lock bypass, result tampering | NetworkPolicy, consider AUTH + TLS |
| Redis | CodingResult tampering | Inject malicious patch into commit | base_sha validation, path traversal scan, MR review gate |
| Copilot SDK | Prompt injection | Exfiltrate env vars via output | Env allowlist, output treated as data |
| K8s Job | Malicious repo code | Token exfiltration | ReadOnlyRootFilesystem, no push access, egress NetworkPolicy |
| K8s Job | Malicious patch injection | Write arbitrary files in MR | Path traversal check, MAX_PATCH_SIZE, git apply validation |
| Repo config | Symlink escape | Read system files | Boundary check in `_resolve_real_path()` |
| Clone URL | SSRF / local file access | Read local files, internal services | HTTPS-only, no embedded creds, git CLI validation |

---

## Recommended Hardening

1. **NetworkPolicy**: Restrict Redis to service + job pods only
2. **Redis AUTH + TLS**: Enable password authentication and encryption in transit
3. **K8s Secrets for Job pods**: Mount credentials via Secret refs, not env vars in configmap
4. **GitLab IP Allowlist**: Restrict `/webhook` endpoint
5. **Project Access Tokens**: Use instead of personal tokens for GITLAB_TOKEN
6. **GitHub App Tokens**: Use instead of PATs for GITHUB_TOKEN
7. **Audit Logging**: Enable GitLab/Jira API audit logs
8. **Rotate Secrets**: Quarterly rotation of all tokens/keys
9. **Egress NetworkPolicy**: Block job pod egress to internal services (allow GitLab, Copilot API, Redis only)
10. **Resource Quotas**: Limit job pod resource consumption
11. **Pin Docker Base Images**: Use digest-based pins to prevent supply chain attacks ✅ (implemented — `Dockerfile` uses `@sha256:` pins, CI validates, Dependabot updates)
12. **Review Gate for /copilot**: Require human approval before auto-push on coding commands (prevents prompt injection → auto-merge)

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
3. Copilot API compromise can generate malicious reviews or code patches (mitigate: manual MR review before merge, patch validation)
4. HMAC secret compromise allows full webhook replay (mitigate: rotation, monitoring)
5. Prompt injection in `/copilot` command could instruct agent to write malicious code — the patch lands as a MR, not directly on main (mitigate: require human review before merge)
