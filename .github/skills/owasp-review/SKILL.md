---
name: owasp-review
description: Step-by-step OWASP Top 10 security review checklist. Use this when reviewing code for security issues, submitting a PR, or when code touches authentication, authorization, data storage, or external communication.
---

Before submitting a PR, review the code against each OWASP Top 10 category. Flag any findings in the PR description.

## Checklist

### 1. Broken Access Control
- [ ] Are authorization checks enforced on every endpoint/function that requires them?
- [ ] Can a user access another user's data by manipulating IDs or parameters?
- [ ] Are default-deny policies in place?

### 2. Cryptographic Failures
- [ ] Is sensitive data encrypted in transit (TLS) and at rest?
- [ ] Are strong, current algorithms used (no MD5, SHA1 for security purposes)?
- [ ] Are keys/secrets stored securely (not in code, not in logs)?

### 3. Injection
- [ ] Are all inputs validated and sanitized?
- [ ] Are parameterized queries used for all database access?
- [ ] Is user input ever concatenated into commands, queries, or templates?

### 4. Insecure Design
- [ ] Are trust boundaries clearly defined?
- [ ] Is input from untrusted sources treated differently from trusted sources?
- [ ] Are rate limits and resource constraints in place where needed?

### 5. Security Misconfiguration
- [ ] Are default credentials, keys, or configs changed?
- [ ] Are error messages generic (no stack traces or internal details exposed)?
- [ ] Are unnecessary features, ports, or services disabled?

### 6. Vulnerable and Outdated Components
- [ ] Are all dependencies pinned to specific versions?
- [ ] Are there known vulnerabilities in any dependency?
- [ ] Is there a process to update dependencies?

### 7. Identification and Authentication Failures
- [ ] Are passwords hashed with a strong algorithm (bcrypt, argon2)?
- [ ] Is multi-factor authentication supported where appropriate?
- [ ] Are session tokens generated securely and invalidated on logout?

### 8. Software and Data Integrity Failures
- [ ] Are CI/CD pipelines protected from tampering?
- [ ] Are dependencies verified (checksums, signatures)?
- [ ] Is deserialization of untrusted data avoided or protected?

### 9. Security Logging and Monitoring Failures
- [ ] Are authentication attempts (success and failure) logged?
- [ ] Are authorization failures logged?
- [ ] Are logs protected from tampering and injection?
- [ ] Do logs avoid capturing sensitive data (passwords, tokens, PII)?

### 10. Server-Side Request Forgery (SSRF)
- [ ] Are outbound requests validated against an allowlist?
- [ ] Is user input ever used to construct URLs for server-side requests?
- [ ] Are internal network addresses blocked from user-supplied URLs?

### 11. Container Security (if applicable)
- [ ] Is the Docker socket mounted? (equivalent to root access on host — document justification)
- [ ] Is `--privileged` used? (document why and whether it can be scoped down)
- [ ] Are environment variables propagated to child containers? (no service secrets to sandboxed processes)
- [ ] Are base images pinned to digests, not mutable tags like `latest`?
- [ ] Is the container filesystem read-only where possible? (`--read-only` + targeted `--tmpfs`)
- [ ] Are capabilities dropped? (`--cap-drop=ALL`, add back only what's needed)
- [ ] Are resource limits set? (`--cpus`, `--memory`, `--pids-limit`)

## Output

Add a section to the PR description:

```
## OWASP Self-Review
- [x] Broken Access Control — N/A (no auth changes)
- [x] Injection — parameterized queries used throughout
- [ ] SSRF — needs review: user-supplied URL in webhook config
```
