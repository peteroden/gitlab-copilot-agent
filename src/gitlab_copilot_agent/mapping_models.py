"""YAML-first mapping models for Jira→GitLab project bindings.

This module defines two layers of models:

1. **YAML source models** — what the user edits (``MappingFile``, ``Binding``).
2. **Rendered JSON models** — what the runtime consumes (``RenderedMap``,
   ``RenderedBinding``), produced by ``MappingFile.render()``.

The helper CLI (``mapping_cli``) reads a YAML file, validates it using these
models, and emits the rendered JSON that populates ``JIRA_PROJECT_MAP``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Defaults(BaseModel):
    """Default values applied to every binding unless overridden."""

    model_config = ConfigDict(strict=True)

    target_branch: str = Field(
        default="main",
        description="Default MR target branch applied to all bindings",
    )
    credential_ref: str = Field(
        default="default",
        description="Default credential alias; maps to GITLAB_TOKEN",
    )


class Binding(BaseModel):
    """A single Jira-project → GitLab-repo binding as written in YAML."""

    model_config = ConfigDict(strict=True)

    jira_project: str = Field(
        description="Jira project key, e.g. 'PROJ'",
        min_length=1,
    )
    repo: str = Field(
        description="GitLab repo path, e.g. 'group/service-a'",
        min_length=1,
    )
    target_branch: str | None = Field(
        default=None,
        description="Override the default target branch for this binding",
    )
    credential_ref: str | None = Field(
        default=None,
        description="Override the default credential alias for this binding",
    )

    @model_validator(mode="after")
    def _validate_repo_path(self) -> Binding:
        if "/" not in self.repo:
            msg = (
                f"repo '{self.repo}' must contain at least one '/' "
                f"(expected format: 'group/project')"
            )
            raise ValueError(msg)
        return self


class MappingFile(BaseModel):
    """Top-level YAML mapping file that the user edits.

    Example YAML::

        defaults:
          target_branch: main
          credential_ref: default

        bindings:
          - jira_project: PROJ
            repo: group/service-a
          - jira_project: OPS
            repo: group/platform-tools
            target_branch: develop
            credential_ref: platform_team
    """

    model_config = ConfigDict(strict=True)

    defaults: Defaults = Field(default_factory=Defaults)
    bindings: list[Binding] = Field(
        description="List of Jira→GitLab bindings",
        min_length=1,
    )

    @model_validator(mode="after")
    def _check_duplicate_jira_keys(self) -> MappingFile:
        seen: dict[str, int] = {}
        duplicates: list[str] = []
        for idx, b in enumerate(self.bindings):
            key = b.jira_project
            if key in seen:
                duplicates.append(f"'{key}' at binding {seen[key]} and {idx}")
            seen[key] = idx
        if duplicates:
            msg = f"Duplicate Jira project keys: {'; '.join(duplicates)}"
            raise ValueError(msg)
        return self

    def required_credential_refs(self) -> set[str]:
        """Return all credential aliases referenced by bindings."""
        refs: set[str] = set()
        for b in self.bindings:
            refs.add(b.credential_ref or self.defaults.credential_ref)
        return refs

    def render(self) -> RenderedMap:
        """Produce the runtime JSON structure consumed by the app."""
        rendered_bindings: dict[str, RenderedBinding] = {}
        for b in self.bindings:
            rendered_bindings[b.jira_project] = RenderedBinding(
                repo=b.repo,
                target_branch=b.target_branch or self.defaults.target_branch,
                credential_ref=b.credential_ref or self.defaults.credential_ref,
            )
        return RenderedMap(mappings=rendered_bindings)


class RenderedBinding(BaseModel):
    """A single resolved binding in the runtime JSON."""

    model_config = ConfigDict(strict=True)

    repo: str = Field(description="GitLab repo path")
    target_branch: str = Field(description="Resolved MR target branch")
    credential_ref: str = Field(description="Resolved credential alias")


class RenderedMap(BaseModel):
    """Runtime JSON structure that populates ``JIRA_PROJECT_MAP``."""

    model_config = ConfigDict(strict=True)

    mappings: dict[str, RenderedBinding] = Field(
        description="Map of Jira project key → resolved binding",
    )
