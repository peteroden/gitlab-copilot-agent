# GitLab Copilot Agent — Developer Documentation

**Purpose**: AI-powered code review and task execution for GitLab merge requests and Jira issues using GitHub Copilot SDK.

This documentation provides comprehensive implementation details for developers and automated systems performing architecture reviews, threat modeling, and security audits.

---

## Architecture

- **[Architecture Overview](architecture-overview.md)** — System components, external dependencies, trust boundaries, deployment topology
- **[Request Flows](request-flows.md)** — End-to-end data flows for webhooks, polling, and task execution with sequence diagrams
- **[Concurrency & State](concurrency-and-state.md)** — Distributed locking, deduplication, watermarks, and race condition prevention
- **[Task Execution](task-execution.md)** — LocalTaskExecutor vs KubernetesTaskExecutor, prompt construction, SDK integration

---

## References

- **[Module Reference](module-reference.md)** — Every .py file: purpose, key classes/functions, dependencies
- **[Data Models](data-models.md)** — All Pydantic models: fields, types, relationships, validation rules
- **[Configuration Reference](configuration-reference.md)** — Every environment variable: type, default, validation, required/optional
- **[Security Model](security-model.md)** — Trust boundaries, authentication, input validation, sandbox isolation, secret handling
- **[Observability](observability.md)** — OTEL setup, all 7 metrics, structured logging, trace correlation

---

## Guides

- **[Testing Guide](testing-guide.md)** — Test structure, shared fixtures, mocking patterns, coverage requirements
- **[Deployment Guide](deployment-guide.md)** — Docker build, Helm chart, k3d local dev, health checks, scaling

---

## Quick Links

| Flow | Entry Point | Processing | External Service |
|------|-------------|------------|------------------|
| MR Review (webhook) | `webhook.py` | `orchestrator.py` → `review_engine.py` → `copilot_session.py` | GitLab API, Copilot SDK |
| MR Review (poller) | `gitlab_poller.py` | → `orchestrator.py` | GitLab API |
| /copilot command | `webhook.py` | `mr_comment_handler.py` → `copilot_session.py` | GitLab API, Copilot SDK |
| Jira coding task | `jira_poller.py` | `coding_orchestrator.py` → `coding_engine.py` → `copilot_session.py` | Jira API, GitLab API, Copilot SDK |

---

## Key Concepts

- **Trust Boundary**: Untrusted input (webhooks, GitLab API responses, repo contents) vs. trusted internal state (Redis, app memory)
- **Deduplication**: Reviews tracked by `(project_id, mr_iid, head_sha)`; notes by `(project_id, mr_iid, note_id)`; Jira issues by issue key
- **Locking**: Per-repo serialization using `git_http_url` as key to prevent concurrent clones/pushes
- **Watermark**: GitLab poller tracks `updated_after` timestamp to avoid replaying historical events
- **Sandbox**: K8s executor isolates tasks in ephemeral pods; local executor runs SDK in-process with minimal env vars

---

**Version**: 0.1.0 | **Python**: 3.12+ | **Dependencies**: See [architecture-overview.md](architecture-overview.md)
