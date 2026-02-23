"""Tests for the config generator module."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from demo_provision.config_generator import (
    generate_project_map,
    generate_webhook_secret,
    print_config_output,
)

from .conftest import (
    GITLAB_PROJECT_ID,
    GITLAB_PROJECT_PATH,
    GITLAB_PROJECT_URL,
    GITLAB_URL,
    JIRA_PROJECT_KEY,
    JIRA_URL,
)

GITLAB_CLONE_URL = f"{GITLAB_URL}/{GITLAB_PROJECT_PATH}.git"


class TestGenerateProjectMap:
    def test_produces_valid_json(self) -> None:
        result = generate_project_map(JIRA_PROJECT_KEY, GITLAB_PROJECT_ID, GITLAB_CLONE_URL)

        parsed = json.loads(result)
        assert "mappings" in parsed
        assert JIRA_PROJECT_KEY in parsed["mappings"]
        entry = parsed["mappings"][JIRA_PROJECT_KEY]
        assert entry["gitlab_project_id"] == GITLAB_PROJECT_ID
        assert entry["clone_url"] == GITLAB_CLONE_URL
        assert entry["target_branch"] == "main"

    def test_custom_target_branch(self) -> None:
        result = generate_project_map(
            JIRA_PROJECT_KEY, GITLAB_PROJECT_ID, GITLAB_CLONE_URL, "develop"
        )

        parsed = json.loads(result)
        assert parsed["mappings"][JIRA_PROJECT_KEY]["target_branch"] == "develop"


class TestGenerateWebhookSecret:
    def test_returns_nonempty_string(self) -> None:
        secret = generate_webhook_secret()

        assert isinstance(secret, str)
        assert len(secret) > 20

    def test_returns_unique_values(self) -> None:
        secrets = {generate_webhook_secret() for _ in range(10)}

        assert len(secrets) == 10


class TestPrintConfigOutput:
    def test_prints_all_required_sections(self, capsys: object) -> None:
        import builtins

        captured_lines: list[str] = []
        original_print = builtins.print

        def capture_print(*args: object, **kwargs: object) -> None:
            captured_lines.append(" ".join(str(a) for a in args))

        builtins.print = capture_print  # type: ignore[assignment]
        try:
            print_config_output(
                gitlab_url=GITLAB_URL,
                gitlab_project_url=GITLAB_PROJECT_URL,
                gitlab_project_path=GITLAB_PROJECT_PATH,
                gitlab_project_id=GITLAB_PROJECT_ID,
                jira_url=JIRA_URL,
                jira_project_key=JIRA_PROJECT_KEY,
                jira_issue_keys=["DEMO-1", "DEMO-2", "DEMO-3"],
                webhook_secret="test-secret",
            )
        finally:
            builtins.print = original_print  # type: ignore[assignment]

        output = "\n".join(captured_lines)
        assert "JIRA_PROJECT_MAP" in output
        assert GITLAB_PROJECT_URL in output
        assert JIRA_PROJECT_KEY in output
        assert "NEXT STEPS" in output
        assert "CLEANUP" in output
        assert "webhook" in output.lower()
