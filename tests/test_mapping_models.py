"""Tests for YAML mapping models — validation, defaults, rendering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gitlab_copilot_agent.mapping_models import (
    Binding,
    Defaults,
    MappingFile,
    RenderedBinding,
    RenderedMap,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JIRA_KEY_PROJ = "PROJ"
JIRA_KEY_OPS = "OPS"
REPO_SERVICE_A = "group/service-a"
REPO_PLATFORM = "group/platform-tools"
DEFAULT_BRANCH = "main"
DEVELOP_BRANCH = "develop"
DEFAULT_CRED = "default"
PLATFORM_CRED = "platform_team"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_binding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"jira_project": JIRA_KEY_PROJ, "repo": REPO_SERVICE_A}
    return {**base, **overrides}


def _minimal_mapping(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"bindings": [_minimal_binding()]}
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_defaults_have_sensible_values(self) -> None:
        d = Defaults()
        assert d.target_branch == DEFAULT_BRANCH
        assert d.credential_ref == DEFAULT_CRED

    def test_defaults_override(self) -> None:
        d = Defaults(target_branch=DEVELOP_BRANCH, credential_ref=PLATFORM_CRED)
        assert d.target_branch == DEVELOP_BRANCH
        assert d.credential_ref == PLATFORM_CRED


# ---------------------------------------------------------------------------
# Binding validation
# ---------------------------------------------------------------------------


class TestBinding:
    def test_valid_binding(self) -> None:
        b = Binding(**_minimal_binding())
        assert b.jira_project == JIRA_KEY_PROJ
        assert b.repo == REPO_SERVICE_A
        assert b.target_branch is None
        assert b.credential_ref is None

    def test_binding_with_overrides(self) -> None:
        b = Binding(**_minimal_binding(target_branch=DEVELOP_BRANCH, credential_ref=PLATFORM_CRED))
        assert b.target_branch == DEVELOP_BRANCH
        assert b.credential_ref == PLATFORM_CRED

    def test_repo_must_contain_slash(self) -> None:
        with pytest.raises(ValidationError, match="must contain at least one '/'"):
            Binding(**_minimal_binding(repo="noslash"))

    def test_repo_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            Binding(**_minimal_binding(repo=""))

    def test_jira_project_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            Binding(**_minimal_binding(jira_project=""))


# ---------------------------------------------------------------------------
# MappingFile validation
# ---------------------------------------------------------------------------


class TestMappingFile:
    def test_minimal_mapping(self) -> None:
        m = MappingFile(**_minimal_mapping())
        assert len(m.bindings) == 1
        assert m.defaults.target_branch == DEFAULT_BRANCH

    def test_bindings_required(self) -> None:
        with pytest.raises(ValidationError):
            MappingFile(bindings=[])

    def test_duplicate_jira_keys_rejected(self) -> None:
        data = _minimal_mapping(
            bindings=[
                _minimal_binding(),
                _minimal_binding(repo="other/repo"),
            ]
        )
        with pytest.raises(ValidationError, match="Duplicate Jira project keys"):
            MappingFile(**data)

    def test_two_distinct_bindings(self) -> None:
        data = _minimal_mapping(
            bindings=[
                _minimal_binding(),
                _minimal_binding(jira_project=JIRA_KEY_OPS, repo=REPO_PLATFORM),
            ]
        )
        m = MappingFile(**data)
        assert len(m.bindings) == 2


# ---------------------------------------------------------------------------
# required_credential_refs
# ---------------------------------------------------------------------------


class TestCredentialRefs:
    def test_default_only(self) -> None:
        m = MappingFile(**_minimal_mapping())
        assert m.required_credential_refs() == {DEFAULT_CRED}

    def test_mixed_refs(self) -> None:
        data = _minimal_mapping(
            bindings=[
                _minimal_binding(),
                _minimal_binding(
                    jira_project=JIRA_KEY_OPS,
                    repo=REPO_PLATFORM,
                    credential_ref=PLATFORM_CRED,
                ),
            ]
        )
        m = MappingFile(**data)
        assert m.required_credential_refs() == {DEFAULT_CRED, PLATFORM_CRED}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_render_applies_defaults(self) -> None:
        m = MappingFile(**_minimal_mapping())
        rendered = m.render()
        assert isinstance(rendered, RenderedMap)
        rb = rendered.mappings[JIRA_KEY_PROJ]
        assert isinstance(rb, RenderedBinding)
        assert rb.repo == REPO_SERVICE_A
        assert rb.target_branch == DEFAULT_BRANCH
        assert rb.credential_ref == DEFAULT_CRED

    def test_render_uses_overrides(self) -> None:
        data = _minimal_mapping(
            defaults={"target_branch": DEFAULT_BRANCH, "credential_ref": DEFAULT_CRED},
            bindings=[
                _minimal_binding(target_branch=DEVELOP_BRANCH, credential_ref=PLATFORM_CRED),
            ],
        )
        m = MappingFile(**data)
        rendered = m.render()
        rb = rendered.mappings[JIRA_KEY_PROJ]
        assert rb.target_branch == DEVELOP_BRANCH
        assert rb.credential_ref == PLATFORM_CRED

    def test_render_multi_binding(self) -> None:
        data = _minimal_mapping(
            bindings=[
                _minimal_binding(),
                _minimal_binding(
                    jira_project=JIRA_KEY_OPS,
                    repo=REPO_PLATFORM,
                    target_branch=DEVELOP_BRANCH,
                    credential_ref=PLATFORM_CRED,
                ),
            ],
        )
        m = MappingFile(**data)
        rendered = m.render()
        assert set(rendered.mappings.keys()) == {JIRA_KEY_PROJ, JIRA_KEY_OPS}
        assert rendered.mappings[JIRA_KEY_OPS].target_branch == DEVELOP_BRANCH

    def test_rendered_json_round_trip(self) -> None:
        m = MappingFile(**_minimal_mapping())
        rendered = m.render()
        json_str = rendered.model_dump_json()
        reloaded = RenderedMap.model_validate_json(json_str)
        assert reloaded == rendered
