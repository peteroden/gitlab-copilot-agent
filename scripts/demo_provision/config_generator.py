"""Config generator — outputs JIRA_PROJECT_MAP and .env snippets."""

from __future__ import annotations

import json
import secrets


def generate_project_map(
    jira_project_key: str,
    gitlab_project_path: str,
    target_branch: str = "main",
) -> str:
    """Generate JIRA_PROJECT_MAP JSON string."""
    mapping = {
        jira_project_key: {
            "gitlab_project": gitlab_project_path,
            "target_branch": target_branch,
        }
    }
    return json.dumps(mapping)


def generate_webhook_secret() -> str:
    """Generate a random webhook secret."""
    return secrets.token_urlsafe(32)


def print_config_output(
    *,
    gitlab_url: str,
    gitlab_project_url: str,
    gitlab_project_path: str,
    jira_url: str,
    jira_project_key: str,
    jira_issue_keys: list[str],
    webhook_secret: str,
    webhook_url: str | None = None,
    webhook_configured: bool = False,
) -> None:
    """Print configuration output and next steps."""
    project_map = generate_project_map(jira_project_key, gitlab_project_path)

    print("\n" + "=" * 60)
    print("  DEMO ENVIRONMENT PROVISIONED SUCCESSFULLY")
    print("=" * 60)

    print(f"\n✅ GitLab project: {gitlab_project_url}")
    print(f"✅ Jira project:   {jira_url}/projects/{jira_project_key}")
    print(f"✅ Jira issues:    {', '.join(jira_issue_keys)}")
    if webhook_configured:
        print(f"✅ Webhook:        configured → {webhook_url}/webhook")

    print("\n--- CONFIGURATION (add to .env) ---\n")
    print(f"GITLAB_URL={gitlab_url}")
    print("GITLAB_TOKEN=<your-token>")
    print(f"GITLAB_WEBHOOK_SECRET={webhook_secret}")
    print(f"JIRA_URL={jira_url}")
    print("JIRA_EMAIL=<your-email>")
    print("JIRA_API_TOKEN=<your-token>")
    print(f"JIRA_PROJECT_MAP='{project_map}'")

    print("\n--- NEXT STEPS ---\n")

    step = 1
    if not webhook_configured:
        print(f"{step}. Configure GitLab webhook:")
        print("   URL:    <your-agent-url>/webhook")
        print(f"   Secret: {webhook_secret}")
        print("   Events: Merge request events, Comment events")
        print(f"   Go to:  {gitlab_project_url}/-/hooks")
        step += 1

    print(f"{step}. Start the agent service:")
    print("   uv run uvicorn gitlab_copilot_agent.main:app --port 8000")
    step += 1

    print(f"\n{step}. Demo: Jira → GitLab flow")
    print(f"   Open {jira_url}/browse/{jira_issue_keys[0]}")
    print('   Move to "AI Ready" → agent creates branch + MR automatically')
    step += 1

    print(f"\n{step}. Demo: /copilot command")
    print("   Comment on any MR: /copilot fix the type hints in models.py")
    print("   Agent edits the code and pushes a commit")
    step += 1

    print(f"\n{step}. Demo: Repo config discovery")
    print(f"   Show {gitlab_project_url}/-/blob/main/AGENTS.md")
    print(f"   Show {gitlab_project_url}/-/blob/main/.github/skills/security-patterns/SKILL.md")
    print('   Narrate: "The agent enforces YOUR project rules, not generic AI"')

    print("\n--- CLEANUP ---\n")
    print(f"GitLab: {gitlab_project_url}/-/settings/general (Advanced → Delete)")
    print(f"Jira:   {jira_url}/jira/settings/projects (archive or delete {jira_project_key})")
    print()
