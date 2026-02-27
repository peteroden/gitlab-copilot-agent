"""Tests for prompt_defaults — configurable prompt resolution."""

from gitlab_copilot_agent.prompt_defaults import (
    DEFAULT_CODING_PROMPT,
    DEFAULT_MR_COMMENT_PROMPT,
    DEFAULT_REVIEW_PROMPT,
    get_prompt,
)
from tests.conftest import make_settings


class TestDefaults:
    """Built-in defaults are returned when no overrides are set."""

    def test_coding_default(self) -> None:
        result = get_prompt(make_settings(), "coding")
        assert result == DEFAULT_CODING_PROMPT

    def test_review_default(self) -> None:
        result = get_prompt(make_settings(), "review")
        assert result == DEFAULT_REVIEW_PROMPT

    def test_mr_comment_default(self) -> None:
        result = get_prompt(make_settings(), "mr_comment")
        assert result == DEFAULT_MR_COMMENT_PROMPT

    def test_mr_comment_default_matches_coding(self) -> None:
        assert DEFAULT_MR_COMMENT_PROMPT == DEFAULT_CODING_PROMPT


class TestOverride:
    """Full override replaces the built-in default entirely."""

    def test_coding_override(self) -> None:
        s = make_settings(coding_system_prompt="custom coding")
        assert get_prompt(s, "coding") == "custom coding"

    def test_review_override(self) -> None:
        s = make_settings(review_system_prompt="custom review")
        assert get_prompt(s, "review") == "custom review"

    def test_mr_comment_override(self) -> None:
        s = make_settings(mr_comment_system_prompt="custom mr")
        assert get_prompt(s, "mr_comment") == "custom mr"

    def test_override_ignores_suffix(self) -> None:
        s = make_settings(
            coding_system_prompt="override",
            coding_system_prompt_suffix="suffix",
        )
        result = get_prompt(s, "coding")
        assert result == "override"
        assert "suffix" not in result


class TestSuffix:
    """Suffix is appended to the built-in default."""

    def test_coding_suffix(self) -> None:
        s = make_settings(coding_system_prompt_suffix="extra rules")
        result = get_prompt(s, "coding")
        assert result.startswith(DEFAULT_CODING_PROMPT)
        assert result.endswith("extra rules")

    def test_review_suffix(self) -> None:
        s = make_settings(review_system_prompt_suffix="extra review rules")
        result = get_prompt(s, "review")
        assert result.startswith(DEFAULT_REVIEW_PROMPT)
        assert result.endswith("extra review rules")


class TestGlobalPrompt:
    """Global SYSTEM_PROMPT is prepended to all persona prompts."""

    def test_global_prepend(self) -> None:
        s = make_settings(system_prompt="You work for Acme Corp.")
        result = get_prompt(s, "coding")
        assert result.startswith("You work for Acme Corp.")
        assert DEFAULT_CODING_PROMPT in result

    def test_global_with_type_override(self) -> None:
        s = make_settings(
            system_prompt="Global preamble",
            coding_system_prompt="Custom coding",
        )
        result = get_prompt(s, "coding")
        assert result == "Global preamble\n\nCustom coding"

    def test_global_suffix_only(self) -> None:
        s = make_settings(system_prompt_suffix="global suffix")
        result = get_prompt(s, "coding")
        assert result.startswith("global suffix")
        assert DEFAULT_CODING_PROMPT in result

    def test_global_override_and_suffix_combine(self) -> None:
        s = make_settings(
            system_prompt="override",
            system_prompt_suffix="suffix",
        )
        result = get_prompt(s, "coding")
        assert result.startswith("override\n\nsuffix")
        assert DEFAULT_CODING_PROMPT in result

    def test_global_empty_by_default(self) -> None:
        """When no global prompt is set, output is just the type-specific prompt."""
        result = get_prompt(make_settings(), "coding")
        assert result == DEFAULT_CODING_PROMPT
        assert not result.startswith("\n")


class TestCodingPromptContent:
    """Verify #191 fix — coding prompt prioritizes repo standards."""

    def test_mentions_repo_config_precedence(self) -> None:
        assert "AGENTS.md" in DEFAULT_CODING_PROMPT
        assert "skills" in DEFAULT_CODING_PROMPT
        assert "instructions" in DEFAULT_CODING_PROMPT

    def test_warns_against_anti_patterns(self) -> None:
        assert "SQL injection" in DEFAULT_CODING_PROMPT
        assert "hardcoded secrets" in DEFAULT_CODING_PROMPT

    def test_preserves_style_convention_guidance(self) -> None:
        assert "code style" in DEFAULT_CODING_PROMPT
        assert "formatting" in DEFAULT_CODING_PROMPT
        assert "architecture" in DEFAULT_CODING_PROMPT
