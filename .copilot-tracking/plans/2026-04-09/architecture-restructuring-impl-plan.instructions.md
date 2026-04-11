---
applyTo: '.copilot-tracking/changes/2026-04-09/architecture-restructuring-changes.md'
---
<!-- markdownlint-disable-file -->
# Implementation Plan: Architecture Restructuring

## Overview

Incremental restructuring of gitlab-copilot-agent (~8k LOC, 42 modules) from organic growth into a clean, protocol-based architecture with typed DI, GitLab-centric config, unified pipelines, end-to-end tracing, and security hardening. 25 steps across 10 phases, each independently deployable and verified by unit tests, K3d E2E, and ACA integration tests.

## Objectives

### User Requirements

* Replace `app.state` service locator with typed AppContext — Source: Architecture plan R1
* New GitLab-centric config schema (`projects[]` + `integrations[]`) with JSON Schema — Source: Architecture plan S4, R7
* Unify K8s/ACA executors into shared base class — Source: Architecture plan R2
* Add `dispatch_backend="local"` for standalone local mode — Source: Architecture plan R13
* Drop python-gitlab, rewrite GitLabClient with httpx (async-native) — Source: Architecture plan R9, S12 #4
* Internal TaskEvent model replacing webhook payload synthesis — Source: Architecture plan R3
* Unified dedup service replacing 4 divergent mechanisms — Source: Architecture plan R4, Issues #327, #329
* Pipeline protocol with shared stages (prepare → execute → process → cleanup) — Source: Architecture plan R5
* Prompt strategy toggle (inline vs file-based context) — Source: Architecture plan S12 #3
* Split oversized modules, extract BaseSettings mixin — Source: Architecture plan R6, R8, Issue #326
* End-to-end tracing with W3C traceparent propagation — Source: Architecture plan R11, Issues #351, #352
* Security hardening: prompt injection, ingress restriction, rate limiting, review gate — Source: Architecture plan R12, Issues #350, #354, #284, #156
* Container image split: controller vs task runner — Source: Issue #289

### Derived Objectives

* Enable ruff rules (D, ANN, N, TCH, PTH, RET, RUF, PLE, PLW) with baseline ratchet — Derived from: Architecture plan S8
* Delete legacy modules (project_mapping.py, process_sandbox.py) — Derived from: Architecture plan R10
* Google-style docstrings for all public functions (audience: LLM coders + new collaborators) — Derived from: Architecture plan S8
* TDD practice: write failing tests first for every change — Derived from: Architecture plan S6
* Tag `pre-refactor` on main before starting — Derived from: Architecture plan S11
* ADRs for each major design decision (0010–0018) — Derived from: Architecture plan

## Context Summary

### Project Files

* src/gitlab_copilot_agent/main.py — Lifespan bootstrap, app.state service locator, _create_executor() factory (lines 124-264)
* src/gitlab_copilot_agent/config.py — Settings, TaskRunnerSettings (~60% field duplication), JiraSettings (lines 35-402)
* src/gitlab_copilot_agent/mapping_models.py — MappingFile, Binding, Defaults, RenderedMap, RenderedBinding — Jira-keyed (210 LOC)
* src/gitlab_copilot_agent/mapping_cli.py — validate, show, render-json subcommands (137 LOC)
* src/gitlab_copilot_agent/project_registry.py — ProjectRegistry, ResolvedProject, from_rendered_map() requires RenderedMap (91 LOC)
* src/gitlab_copilot_agent/credential_registry.py — CredentialRegistry, GITLAB_TOKEN__* env pattern (127 LOC)
* src/gitlab_copilot_agent/webhook.py — Webhook endpoint, 6 getattr(app.state, ...) calls (290 LOC)
* src/gitlab_copilot_agent/gitlab_poller.py — Synthesizes MergeRequestWebhookPayload, hardcoded _interval (361 LOC)
* src/gitlab_copilot_agent/jira_poller.py — ProcessedIssueTracker in-memory set (130 LOC)
* src/gitlab_copilot_agent/orchestrator.py — handle_review() 188-line function, creates own GitLabClient (225 LOC)
* src/gitlab_copilot_agent/discussion_orchestrator.py — handle_discussion_interaction(), creates own clients (254 LOC)
* src/gitlab_copilot_agent/coding_orchestrator.py — CodingOrchestrator class, only class-based orchestrator (189 LOC)
* src/gitlab_copilot_agent/task_executor.py — TaskExecutor protocol, TaskParams, LocalTaskExecutor (86 LOC)
* src/gitlab_copilot_agent/k8s_executor.py — Claim-check dispatch, ~90% duplicated with ACA (126 LOC)
* src/gitlab_copilot_agent/aca_executor.py — Claim-check dispatch, ~90% duplicated with K8s (134 LOC)
* src/gitlab_copilot_agent/gitlab_client.py — python-gitlab sync wrapper, 12 REST endpoints, to_thread (410 LOC)
* src/gitlab_copilot_agent/jira_client.py — httpx-based, no retry/backoff (116 LOC)
* src/gitlab_copilot_agent/concurrency.py — 4 protocols, 4 memory impls, 2 trackers (314 LOC, 46 functions)
* src/gitlab_copilot_agent/telemetry.py — Logging + tracing, disconnected spans, OTLP disabled (350 LOC)
* src/gitlab_copilot_agent/git_operations.py — Clone, commit, push, tar, validate (460 LOC, 20 functions)
* src/gitlab_copilot_agent/comment_parser.py — Parse LLM review output (178 LOC)
* src/gitlab_copilot_agent/comment_poster.py — Post review to GitLab (246 LOC)
* src/gitlab_copilot_agent/review_engine.py — Prompt construction, resolution detection (358 LOC)
* src/gitlab_copilot_agent/project_mapping.py — LEGACY, superseded by mapping_models.py (33 LOC)
* src/gitlab_copilot_agent/process_sandbox.py — LEGACY since ADR-0002 retired
* tests/conftest.py — Shared constants, fixtures, make_settings() factory
* tests/e2e/aca_integration.py — ACA integration tests against real GitLab/Jira
* .github/workflows/e2e.yml — K3d E2E with mock services (runnable locally)
* .github/workflows/cd-dev.yml — Deploy to ACA dev + integration test

### References

* .copilot-tracking/plans/2026-04-09/architecture-restructuring-plan.instructions.md — Architecture decisions, target designs, coding standards
* .copilot-tracking/research/2026-04-09/architecture-analysis-research.md — Per-module assessments with code line references
* .github/instructions/python.instructions.md — Pydantic, Protocol, pytest, conftest patterns
* .github/copilot-instructions.md — Conventional commits, code review, OWASP review

### Standards References

* .github/copilot-instructions.md — Conventional commits, cross-model code review before every push, OWASP review before every push
* .github/instructions/python.instructions.md — Pydantic models, Protocol interfaces, pytest + pytest-asyncio, conftest factories, ≥90% coverage
* .copilot-tracking/plans/2026-04-09/architecture-restructuring-plan.instructions.md S8 — Google docstrings, ruff config, naming conventions, test boundary rule

### PR Sizing

PRs should be **human-reviewable and independently verifiable** — not rigidly ≤200 lines. Use judgment:

- Pure refactors (moving code, renaming, re-exporting) can be larger since the risk is low
- New logic, protocol definitions, and behavioral changes should be smaller and focused
- If a PR is too large to review in one sitting, split it. If splitting would create a broken intermediate state, keep it together.

**Every PR must be independently deployable and pass all three test layers.** No "part 1 of 2" PRs that break E2E.

---

## Testing Strategy: TDD + E2E at Every Step

**Non-negotiable: every PR must pass both unit tests AND E2E integration tests.** No exceptions, no "I'll fix E2E later." If a refactoring PR breaks E2E, it doesn't merge.

### Three Test Layers Per PR

| Layer | What | How | Gate |
|-------|------|-----|------|
| **Unit (TDD)** | Write tests first, then implement. Test public API only. | `make test` — pytest with ≥90% coverage | Must pass before PR is opened |
| **K3d E2E** | Full stack with mock GitLab/Jira/LLM in local k3d cluster | `make k3d-redeploy` + `tests/e2e/run.sh` locally, or `e2e.yml` in CI | Must pass before push — run locally first, CI confirms |
| **ACA Integration** | Real GitLab + Jira + Copilot against dev ACA environment | `cd-dev.yml` workflow — deploy to ACA dev, run `aca_integration.py` | Must pass in CI on every PR |

### TDD Practice

For each PR:
1. **Write failing tests first** — define expected behavior of the new/changed public API
2. **Implement** — make tests pass
3. **Refactor** — clean up under green tests
4. Tests target **public functions only** (see S6 Test Boundary Rule)

### E2E Test Evolution

The E2E tests (`aca_integration.py`, k3d mock tests) must evolve alongside the refactor:

- **Phases 0–3**: Internal changes — existing E2E tests pass as-is. Update config fixtures to v2 format in Phase 1.
- **Phase 4**: TaskEvent + dedup are internal. Add E2E coverage for dedup behavior (duplicate webhook sends).
- **Phase 5**: Pipeline protocol is internal. E2E proves identical external behavior — this is the critical checkpoint.
- **Phase 6**: Module splits are transparent. E2E unchanged.
- **Phase 7**: Add trace assertions to E2E (verify trace IDs in logs).
- **Phase 8**: Add security assertions (404 on non-webhook paths, 429 on rate limit).
- **Phase 9**: Update k3d values for split images.

### When E2E Tests Need Updating

If a PR changes external behavior (config format, API responses, error messages, webhook handling), the **same PR** must update the E2E tests. Don't split E2E updates into follow-up PRs.

### Prerequisites

1. **Tag main before starting**: `git tag pre-refactor` on current HEAD — do this before Phase 0
2. **Create GitHub issues**: One per recommendation (R1–R13), umbrella issue linking all 13

## Implementation Checklist

### [ ] Implementation Phase 0: Groundwork

<!-- parallelizable: true -->

* [ ] Step 0.1: Enable ruff rules (D, ANN, N, TCH, PTH, RET, RUF, PLE, PLW) + generate baseline
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 17-98)
* [ ] Step 0.2: Delete legacy modules (project_mapping.py, process_sandbox.py), remove dead imports
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 100-134)
* [ ] Step 0.3: Pin all dependencies to exact versions in pyproject.toml, verify Dockerfile uses immutable digest hashes (S-I5)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines TBD)

### [ ] Implementation Phase 1: Foundation — Config v2 + AppContext (R1, R7)

<!-- parallelizable: false -->

* [ ] Step 1.1: Define Config v2 Pydantic models (ProjectConfig, IntegrationConfig, ConfigFile, Defaults)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 140-341)
* [ ] Step 1.2: Config v2 loading function + mapping-helper CLI updates (validate, show, render-json, schema)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 343-421)
* [ ] Step 1.3: Define AppContext dataclass + factory function + FastAPI Depends()
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 423-522)
* [ ] Step 1.4: Wire AppContext into main.py lifespan + migrate webhook/pollers + add TTL to credential identity cache (S7)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 524-598)
* [ ] Step 1.5: Rewrite ProjectRegistry.from_config(), update ResolvedProject, delete v1 code
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 600-705)

### [ ] Implementation Phase 2: Quick Wins — Executor Unification + Local Mode (R2, R13)

<!-- parallelizable: true -->

* [ ] Step 2.1: Replace k8s_executor.py and aca_executor.py with single RemoteTaskExecutor (no subclasses)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 711-940)
* [ ] Step 2.2: Add dispatch_backend="local" config option, skip Azure validation, wire LocalTaskExecutor
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 942-1015)

### [ ] Implementation Phase 3: Client Lifecycle — httpx GitLabClient + JiraClient (R9)

<!-- parallelizable: false -->

* [ ] Step 3.1: Rewrite GitLabClient with httpx (12 endpoints), remove python-gitlab from all modules and pyproject.toml
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1021-1158)
* [ ] Step 3.2: Add retry/backoff to JiraClient, fix docstrings, add aclose()
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1160-1216)

### [ ] Implementation Phase 4: Internal Event Model + Unified Dedup (R3, R4)

<!-- parallelizable: false -->

* [ ] Step 4.1: Define TaskEvent + ScheduledTask models (token excluded from serialization S1, clone_url validated S9), migrate all triggers to produce TaskEvent
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1222-1339)
* [ ] Step 4.2: Unified DeduplicationService replacing ReviewedMRTracker, ProcessedIssueTracker, ad-hoc finally-block dedup
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1341-1448)

### [ ] Implementation Phase 5: Pipeline Protocol (R5)

<!-- parallelizable: false -->

* [ ] Step 5.1: Define Pipeline protocol + PipelineContext + run_pipeline(), convert ReviewPipeline
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1454-1634)
* [ ] Step 5.2: Convert DiscussionPipeline
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1636-1677)
* [ ] Step 5.3: Convert CodingPipeline + add prompt_strategy toggle (inline | file-based)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1679-1735)

### [ ] Implementation Phase 6: Module Splits (R6, R8)

<!-- parallelizable: true -->

* [ ] Step 6.1: Split git_operations, concurrency, config, task_runner, telemetry into packages + BaseSettings mixin
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1741-1861)
* [ ] Step 6.2: Inline thin orchestrators into callers
  * Eliminate orchestrator.py, discussion_orchestrator.py, coding_orchestrator.py — move GitLabClient creation, trace spans, repo locks, and dedup checks into webhook.py, gitlab_poller.py, and jira_poller.py respectively. Callers import pipelines directly.

### [ ] Implementation Phase 7: End-to-End Observability (R11)

<!-- parallelizable: false -->

* [ ] Step 7.1: End-to-end trace spans in run_pipeline() + W3C traceparent propagation across queue boundary
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1867-1945)
* [ ] Step 7.2: Enable OTLP export when OTEL_EXPORTER_OTLP_ENDPOINT set + structured audit logging (#170, #355)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 1947-1999)

### [ ] Implementation Phase 8: Security Hardening (R12)

<!-- parallelizable: true -->

* [ ] Step 8.1: Prompt injection hardening — ALL THREE engines (review, discussion, coding), security instructions always appended (non-overridable S2), field-level length limits (S12), output validation (#350, S3)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 2005-2093)
* [ ] Step 8.2: Ingress restriction with IP allowlist (GitLab.com: 34.74.90.64/28, 34.74.226.0/24) + proxy-aware rate limiting using X-Forwarded-For (S8, I2, #354, #284)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 2095-2145)
* [ ] Step 8.3: Review gate before auto-push — default=False, explicit opt-in required (S4, #156)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 2147-2195)

### [ ] Implementation Phase 9: Container Image Split (#289)

<!-- parallelizable: false -->

* [ ] Step 9.1: Multi-stage Dockerfile (controller + task runner images), Helm chart updates, CI build updates. Task runner receives ONLY GITHUB_TOKEN + Azure Storage credentials — no GITLAB_TOKEN, JIRA_*, or WEBHOOK_SECRET (S5)
  * Details: .copilot-tracking/details/2026-04-09/architecture-restructuring-details.md (Lines 2201-2284)

## Phase Dependencies

```
Phase 0 → Phase 1 → Phase 2 (parallel: 2.1, 2.2)
                   → Phase 3
                   → Phase 4
                   → Phase 8 (parallel: 8.1, 8.2, 8.3)
Phase 4 → Phase 5
Phase 5 → Phase 6 (parallel: within 6.1)
Phase 5 → Phase 7
Phase 6 + Phase 7 → Phase 9
```

Critical path: 0 → 1 → 4 → 5 → 7 → 9

## Validation Checkpoints

| After Phase | Criteria |
|-------------|----------|
| 0 | `make lint && make test` green. Baseline committed. `pre-refactor` tag exists. K3d E2E passes. |
| 1 | App starts with v2 config. AppContext holds all services. No `getattr(app.state` in source. ProjectRegistry works without Jira. K3d E2E + ACA pass with v2 config. |
| 2 | Single executor base class. App starts with `DISPATCH_BACKEND=local`. All E2E pass. |
| 3 | No `import gitlab` in source. python-gitlab removed from deps. All E2E pass. |
| 4 | All triggers produce TaskEvent. Single dedup service. No old trackers. All E2E pass. |
| 5 | All 3 pipelines implement Pipeline protocol. Old orchestrator files deleted. Prompt toggle works. All E2E pass — **critical: proves identical external behavior.** |
| 6 | No module over 200 LOC. All packages have `__init__.py` with `__all__`. All E2E pass. |
| 7 | Single trace per task. Trace IDs propagate across queue. OTLP exports when configured. All E2E pass. |
| 8 | All 4 security issues addressed. Untrusted content labeled. Rate limiting active. E2E updated. |
| 9 | Two Docker images. Controller has no Node.js. Both pass health checks. All E2E pass with split images. |

## Cross-Cutting: Every Phase

* All three test layers must pass (unit, K3d E2E, ACA integration)
* Ruff baseline count must not increase
* Touched modules brought into docstring/annotation compliance per architecture plan S8
* Test coverage ≥90%
* Cross-model code review before push
* OWASP security review before push
* Commit messages follow conventional commits
* **Documentation updated in the same PR** — any phase that changes module boundaries, data flow, config format, public APIs, or deployment must update the corresponding docs (target: VitePress/Docusaurus on GitHub Pages)

### Documentation Files to Maintain

| Doc | Path | Affected By |
|-----|------|-------------|
| Architecture overview | `docs/wiki/architecture-overview.md` | Phases 1, 2, 5, 6, 9 |
| Configuration reference | `docs/wiki/configuration-reference.md` | Phase 1 (config v2 schema) |
| Module reference | `docs/wiki/module-reference.md` | Phases 2, 3, 4, 5, 6 (all structural changes) |
| Data models | `docs/wiki/data-models.md` | Phases 1, 4 (TaskEvent, config models) |
| Request flows | `docs/wiki/request-flows.md` | Phases 4, 5 (TaskEvent, pipeline protocol) |
| Task execution | `docs/wiki/task-execution.md` | Phases 2, 5 (executor unification, pipeline) |
| Concurrency and state | `docs/wiki/concurrency-and-state.md` | Phases 4, 6 (unified dedup, module splits) |
| Deployment guide | `docs/wiki/deployment-guide.md` | Phases 2, 7, 9 (local mode, OTLP, image split) |
| Observability | `docs/wiki/observability.md` | Phase 7 (end-to-end tracing) |
| Security model | `docs/wiki/security-model.md` | Phase 8 (all hardening) |
| Testing guide | `docs/wiki/testing-guide.md` | Phases 0, 5 (ruff baseline, pipeline test harness) |

### ADRs Required

Each ADR is written **in the same PR** as the code change it documents. Use the `adr-creation` skill.

| ADR | Decision | Phase | Supersedes |
|-----|----------|-------|------------|
| 0010 | GitLab-centric config v2 schema — `projects[]` + `integrations[]` with ref linking, JSON Schema from Pydantic | Phase 1 | Supersedes ADR-0007 |
| 0011 | Typed AppContext replacing `app.state` service locator | Phase 1 | — |
| 0012 | Drop python-gitlab, rewrite GitLabClient with httpx | Phase 3 | — |
| 0013 | Internal TaskEvent model replacing webhook payload synthesis | Phase 4 | — |
| 0014 | Unified DeduplicationService with pluggable backend | Phase 4 | — |
| 0015 | Pipeline protocol with stage-based execution (prepare → execute → process → cleanup) | Phase 5 | Partially supersedes ADR-0001 (flow design) |
| 0016 | Prompt strategy toggle (inline vs file-based context delivery) | Phase 5 | — |
| 0017 | End-to-end tracing with W3C traceparent across queue boundary | Phase 7 | — |
| 0018 | Container image split — controller vs task runner | Phase 9 | — |

### Existing ADR Updates

When a new ADR supersedes an old one, update the old ADR's Status in the same PR.

| Existing ADR | Status After Refactor |
|---|---|
| 0001 (FastAPI + Copilot SDK) | "Partially superseded by ADR-0015 (pipeline protocol replaces flow design; tech stack decisions still hold)" |
| 0002 (Sandbox config) | Already SUPERSEDED — no change |
| 0003 (K8s controller + worker) | Unchanged |
| 0004 (ACA additive path) | Unchanged |
| 0005 (Azure Storage Queue) | Unchanged |
| 0006 (Unified KEDA + Azurite) | Unchanged |
| 0007 (YAML mapping + credentials) | "**SUPERSEDED** by ADR-0010 (config v2)" |
| 0008 (Blob-based repo transfer) | Unchanged |
| 0009 (SHA marker) | Unchanged |
