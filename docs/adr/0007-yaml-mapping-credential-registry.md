# 0007. YAML-First Mapping with Credential Registry and Hot-Reload

## Status

Accepted

## Context

The service maps Jira projects to GitLab repos via `JIRA_PROJECT_MAP`. The original format required users to look up numeric `gitlab_project_id` and construct `clone_url` manually — error-prone and duplicative since both derive from the repo path. All repos shared a single `GITLAB_TOKEN`, preventing multi-team setups. Adding or removing a mapping required a full redeployment.

## Options Considered

### Option A: Keep flat JSON, add token field

- Pros: Minimal change, backward-compatible
- Cons: Still requires manual `gitlab_project_id`/`clone_url` lookup. No defaults mechanism — every binding repeats `target_branch`. No validation tooling.

### Option B: YAML source with rendering pipeline and credential registry

- Pros: Human-friendly YAML with `defaults` block eliminates repetition. CLI validates before deploy. `repo` path is the only required field — project ID and clone URL resolved at startup. Named `credential_ref` maps to `GITLAB_TOKEN__<ALIAS>` env vars for multi-team isolation. Rendered JSON stays as the env var transport, keeping deployment simple.
- Cons: Two formats to understand (YAML source vs rendered JSON). New modules to maintain.

### Option C: Store mappings in external config service (Consul, etcd)

- Pros: Native watch/reload. Centralized config management.
- Cons: New infrastructure dependency. Over-engineered for current scale (< 20 bindings).

## Decision

Option B. The YAML → rendered JSON pipeline gives us human-friendly authoring with machine-friendly deployment. Key components:

- **`mapping_models.py`**: Pydantic models for both YAML source (`MappingSource`) and rendered JSON (`RenderedMap`). Strict validation, duplicate detection.
- **`mapping_cli.py`**: `mapping-helper validate|show|render-json` CLI for offline validation.
- **`credential_registry.py`**: Reads `GITLAB_TOKEN` + `GITLAB_TOKEN__<ALIAS>` from env. Resolves `credential_ref` → token at startup.
- **`project_registry.py`**: `ResolvedProject` carries resolved project ID, clone URL, and token. `from_rendered_map()` resolves repo paths via GitLab API at startup. Rejects duplicate project IDs.
- **`POST /config/reload`**: Accepts new `RenderedMap` JSON, rebuilds registries atomically, clears Jira dedup state. Authenticated via `X-Gitlab-Token` header.

## Consequences

- **Easier**: Adding a project is `repo: group/project` in YAML + `mapping-helper render-json`. Hot-reload avoids redeployment.
- **Easier**: Multi-team token isolation without separate service instances.
- **Harder**: Operators must understand the YAML → JSON rendering step (mitigated by CLI tooling and docs).
- **Risk**: New env vars (`GITLAB_TOKEN__*`) require container restart to take effect — only mapping changes are hot-reloadable.
- **Follow-up needed**: Secondary code paths (`gitlab_poller.py`) still use the global `GITLAB_TOKEN`. Per-project tokens only flow through the Jira polling → coding pipeline.

> **Note**: `mr_comment_handler.py` referenced in the original ADR was replaced by `discussion_orchestrator.py`.
