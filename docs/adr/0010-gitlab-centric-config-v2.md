# 0010. GitLab-Centric Config v2 Schema

## Status

Accepted — Supersedes ADR-0007

## Context

The service configuration was Jira-coupled: the YAML mapping file (`mapping_models.py`) organized projects by Jira project key, making it impossible to configure GitLab-only (review/discussion) projects without a Jira integration. Non-secret settings were split between the YAML mapping and ~40 environment variables in `config.py`, with no schema validation for the env vars and no way to override Copilot model or prompt settings per-project.

Operators needed:
- Projects without Jira (webhook-triggered review only)
- Per-project Copilot model and plugin overrides
- A single config file with schema validation for CI/CD pre-deploy checks
- Named integrations referenced by projects (decoupling project identity from integration type)

## Options Considered

### Option A: Extend existing YAML mapping with optional Jira

Add optional fields to the existing `MappingFile` / `Binding` models. Keep Jira key as the dict key but allow `null`.

- Pros: Minimal change, backward-compatible
- Cons: Jira key as dict key is semantically wrong for non-Jira projects. No natural place for per-project Copilot overrides, server config, or dispatch settings. Still requires env vars for everything else.

### Option B: GitLab-centric config file with named integrations

New `config_v2.py` with `ConfigFile` root model. Projects keyed by GitLab repo path. Integrations are named blocks referenced by projects. Service-level settings (dispatch, server, prompts, copilot defaults) move into the YAML file. Secrets stay as env vars.

- Pros: Projects are first-class. Non-Jira projects are natural. Per-project overrides for copilot, polling, credentials. JSON Schema from Pydantic for CI/CD validation. Single file for all non-secret config.
- Cons: Migration effort from v1 format. Two config systems coexist during transition.

### Option C: Environment variables only

Move all config to env vars with structured naming (e.g., `PROJECT_0_REPO`, `PROJECT_0_CREDENTIAL_REF`).

- Pros: No YAML parsing. Works everywhere.
- Cons: Unmanageable at scale. No schema validation. No per-project nesting. Array indexing in env vars is fragile.

## Decision

**Option B** — GitLab-centric YAML config with named integrations.

The config file uses `version: 2` and is validated by Pydantic models with `ConfigDict(strict=True)`. Projects reference integrations by name. `ConfigFile.resolve_project()` applies `ConfigDefaults` for omitted fields. `load_config_file()` reads from the `CONFIG_FILE` env var (default: `config.yaml`) and emits S10 audit logs for marketplace URLs.

Key design choices:
- **Secrets stay as env vars** — tokens, connection strings, API keys never appear in the YAML file
- **`projects[]` keyed by repo path** — GitLab identity is primary; Jira is an optional integration
- **`integrations[]` as named blocks** — decouples integration config from project identity; extensible for future integration types
- **Defaults cascade** — `ConfigDefaults` → `ProjectConfig` with `None` = inherit
- **JSON Schema generation** — `ConfigFile.model_json_schema()` for pre-deploy validation via `check-jsonschema`

## Consequences

- v1 mapping format (`mapping_models.py`, `MappingFile`) is preserved for backward compatibility during migration
- `ProjectRegistry.from_config()` added alongside existing `from_rendered_map()`
- `mapping-helper` CLI gains `schema` and `validate-v2` subcommands
- Config v2 is additive in Phase 1; full migration of `Settings` env vars into the YAML file is deferred to later phases
