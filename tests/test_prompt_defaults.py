"""Tests for prompt_defaults — configurable prompt resolution."""

from gitlab_copilot_agent.prompt_defaults import (
    DEFAULT_CODING_PROMPT,
    DEFAULT_DISCUSSION_PROMPT,
    DEFAULT_REVIEW_PROMPT,
    SECURITY_INSTRUCTIONS,
    get_prompt,
)
from tests.conftest import make_settings


class TestDefaults:
    """Built-in defaults are returned when no overrides are set."""

    def test_coding_default(self) -> None:
        result = get_prompt(make_settings(), "coding")
        assert DEFAULT_CODING_PROMPT in result

    def test_review_default(self) -> None:
        result = get_prompt(make_settings(), "review")
        assert DEFAULT_REVIEW_PROMPT in result

    def test_discussion_default(self) -> None:
        result = get_prompt(make_settings(), "discussion")
        assert DEFAULT_DISCUSSION_PROMPT in result


class TestOverride:
    """Full override replaces the built-in default entirely."""

    def test_coding_override(self) -> None:
        s = make_settings(coding_system_prompt="custom coding")
        result = get_prompt(s, "coding")
        assert "custom coding" in result

    def test_review_override(self) -> None:
        s = make_settings(review_system_prompt="custom review")
        result = get_prompt(s, "review")
        assert "custom review" in result

    def test_discussion_override(self) -> None:
        s = make_settings(discussion_system_prompt="custom discussion")
        result = get_prompt(s, "discussion")
        assert "custom discussion" in result

    def test_override_ignores_suffix(self) -> None:
        s = make_settings(
            coding_system_prompt="override",
            coding_system_prompt_suffix="suffix",
        )
        result = get_prompt(s, "coding")
        assert "override" in result
        assert "suffix" not in result


class TestSuffix:
    """Suffix is appended to the built-in default."""

    def test_coding_suffix(self) -> None:
        s = make_settings(coding_system_prompt_suffix="extra rules")
        result = get_prompt(s, "coding")
        assert DEFAULT_CODING_PROMPT in result
        assert "extra rules" in result

    def test_review_suffix(self) -> None:
        s = make_settings(review_system_prompt_suffix="extra review rules")
        result = get_prompt(s, "review")
        assert DEFAULT_REVIEW_PROMPT in result
        assert "extra review rules" in result

    def test_discussion_suffix(self) -> None:
        s = make_settings(discussion_system_prompt_suffix="extra discussion rules")
        result = get_prompt(s, "discussion")
        assert DEFAULT_DISCUSSION_PROMPT in result
        assert "extra discussion rules" in result


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
        assert result.startswith("Global preamble\n\nCustom coding")

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
        """When no global prompt is set, output starts with the type-specific prompt."""
        result = get_prompt(make_settings(), "coding")
        assert result.startswith(DEFAULT_CODING_PROMPT)
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


class TestDiscussionPromptContent:
    """Discussion prompt includes expected structure and intent keywords."""

    def test_mentions_intent_types(self) -> None:
        assert "question" in DEFAULT_DISCUSSION_PROMPT
        assert "coding" in DEFAULT_DISCUSSION_PROMPT
        assert "resolution" in DEFAULT_DISCUSSION_PROMPT

    def test_requires_json_output_for_coding(self) -> None:
        assert '"files_changed"' in DEFAULT_DISCUSSION_PROMPT
        assert '"summary"' in DEFAULT_DISCUSSION_PROMPT
        assert "no JSON block needed" in DEFAULT_DISCUSSION_PROMPT


class TestSecurityInstructions:
    """SECURITY_INSTRUCTIONS is unconditionally appended — no opt-out."""

    def test_present_in_coding_default(self) -> None:
        result = get_prompt(make_settings(), "coding")
        assert SECURITY_INSTRUCTIONS in result

    def test_present_in_review_default(self) -> None:
        result = get_prompt(make_settings(), "review")
        assert SECURITY_INSTRUCTIONS in result

    def test_present_in_discussion_default(self) -> None:
        result = get_prompt(make_settings(), "discussion")
        assert SECURITY_INSTRUCTIONS in result

    def test_present_with_coding_override(self) -> None:
        s = make_settings(coding_system_prompt="totally custom prompt")
        result = get_prompt(s, "coding")
        assert SECURITY_INSTRUCTIONS in result
        assert "totally custom prompt" in result

    def test_present_with_review_override(self) -> None:
        s = make_settings(review_system_prompt="my review rules")
        result = get_prompt(s, "review")
        assert SECURITY_INSTRUCTIONS in result
        assert "my review rules" in result

    def test_present_with_discussion_override(self) -> None:
        s = make_settings(discussion_system_prompt="my discussion rules")
        result = get_prompt(s, "discussion")
        assert SECURITY_INSTRUCTIONS in result
        assert "my discussion rules" in result

    def test_present_with_global_override(self) -> None:
        s = make_settings(system_prompt="Global org rules")
        for prompt_type in ("coding", "review", "discussion"):
            result = get_prompt(s, prompt_type)  # type: ignore[arg-type]
            assert SECURITY_INSTRUCTIONS in result

    def test_present_with_suffix(self) -> None:
        s = make_settings(coding_system_prompt_suffix="extra suffix")
        result = get_prompt(s, "coding")
        assert SECURITY_INSTRUCTIONS in result
        assert "extra suffix" in result

    def test_ends_with_security_instructions(self) -> None:
        """Prompt ends with SECURITY_INSTRUCTIONS — it's the last content."""
        for prompt_type in ("coding", "review", "discussion"):
            result = get_prompt(make_settings(), prompt_type)  # type: ignore[arg-type]
            assert result.endswith(SECURITY_INSTRUCTIONS)

    def test_contains_anti_injection_rules(self) -> None:
        """SECURITY_INSTRUCTIONS contains key anti-injection phrases."""
        assert "UNTRUSTED USER CONTENT" in SECURITY_INSTRUCTIONS
        assert "ignore previous instructions" in SECURITY_INSTRUCTIONS
        assert "prompt injection" in SECURITY_INSTRUCTIONS
