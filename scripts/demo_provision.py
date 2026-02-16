#!/usr/bin/env python3
"""Demo environment provisioner for GitLab Copilot Agent.

Creates a GitLab project with demo code and a Jira project with demo stories,
then outputs the configuration needed to connect them to the agent service.

Usage:
    uv run scripts/demo_provision.py \\
        --gitlab-group myorg \\
        --jira-project-key DEMO

Environment variables required:
    GITLAB_URL, GITLAB_TOKEN, JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

# Add scripts/ to path so demo_provision package is importable
sys.path.insert(0, str(Path(__file__).parent))

from demo_provision.config_generator import (  # noqa: E402
    generate_webhook_secret,
    print_config_output,
)
from demo_provision.gitlab_provisioner import (  # noqa: E402
    create_project as gl_create_project,
)
from demo_provision.gitlab_provisioner import (
    create_webhook,
    get_namespace,
    load_template,
    push_files,
)
from demo_provision.gitlab_provisioner import (
    get_project as gl_get_project,
)
from demo_provision.jira_provisioner import (  # noqa: E402
    DEMO_ISSUES,
    create_issue,
    get_current_user,
)
from demo_provision.jira_provisioner import (
    build_client as jira_build_client,
)
from demo_provision.jira_provisioner import (
    create_project as jira_create_project,
)
from demo_provision.jira_provisioner import (
    get_project as jira_get_project,
)

TEMPLATE_DIR = Path(__file__).parent / "demo_templates" / "blog-api"


def _require_env(name: str) -> str:
    """Get a required environment variable or exit."""
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Error: {name} environment variable is required.", file=sys.stderr)
        sys.exit(1)
    return value


def _detect_ngrok_url() -> str | None:
    """Try to detect ngrok tunnel URL from local API."""
    try:
        resp = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=2.0)
        tunnels = resp.json().get("tunnels", [])
        for tunnel in tunnels:
            if tunnel.get("proto") == "https":
                return tunnel["public_url"]
        if tunnels:
            return tunnels[0]["public_url"]
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision a demo environment for GitLab Copilot Agent."
    )
    parser.add_argument(
        "--gitlab-group",
        required=True,
        help="GitLab group/namespace for the demo project (e.g., myorg)",
    )
    parser.add_argument(
        "--jira-project-key",
        required=True,
        help="Jira project key (e.g., DEMO)",
    )
    parser.add_argument(
        "--gitlab-project-name",
        default="copilot-demo",
        help="GitLab project name (default: copilot-demo)",
    )
    parser.add_argument(
        "--webhook-url",
        default=None,
        help="Agent webhook URL. If omitted, auto-detects from ngrok.",
    )
    parser.add_argument(
        "--trigger-status",
        default="AI Ready",
        help='Jira status that triggers the agent (default: "AI Ready")',
    )
    args = parser.parse_args()

    # Gather credentials
    gitlab_url = _require_env("GITLAB_URL")
    gitlab_token = _require_env("GITLAB_TOKEN")
    jira_url = _require_env("JIRA_URL")
    jira_email = _require_env("JIRA_EMAIL")
    jira_api_token = _require_env("JIRA_API_TOKEN")

    # --- GitLab provisioning ---
    import gitlab

    gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)
    gl.auth()

    project_path = f"{args.gitlab_group}/{args.gitlab_project_name}"
    existing = gl_get_project(gl, project_path)
    if existing:
        print(
            f"Error: GitLab project '{project_path}' already exists.\n"
            f"Delete it or use a different --gitlab-project-name.",
            file=sys.stderr,
        )
        sys.exit(1)

    namespace = get_namespace(gl, args.gitlab_group)
    project = gl_create_project(
        gl,
        name=args.gitlab_project_name,
        namespace_id=namespace.id,
        description="Demo project for GitLab Copilot Agent showcase",
    )

    # Push demo template files
    template_files = load_template(TEMPLATE_DIR)
    push_files(project, "main", template_files, "Initial demo code with intentional issues")
    print(f"✅ GitLab project created: {project.web_url}")
    print(f"   Pushed {len(template_files)} files to main branch")

    # --- Webhook setup ---
    webhook_secret = generate_webhook_secret()
    webhook_url = args.webhook_url
    webhook_configured = False

    if not webhook_url:
        webhook_url = _detect_ngrok_url()
        if webhook_url:
            print(f"✅ Detected ngrok tunnel: {webhook_url}")

    if webhook_url:
        create_webhook(project, f"{webhook_url}/webhook", webhook_secret)
        webhook_configured = True

    # --- Jira provisioning ---
    jira_client = jira_build_client(jira_url, jira_email, jira_api_token)
    try:
        existing_jira = jira_get_project(jira_client, args.jira_project_key)
        if existing_jira:
            print(
                f"Error: Jira project '{args.jira_project_key}' already exists.\n"
                f"Delete it or use a different --jira-project-key.",
                file=sys.stderr,
            )
            sys.exit(1)

        current_user = get_current_user(jira_client)
        lead_account_id = current_user["accountId"]

        jira_create_project(
            jira_client,
            key=args.jira_project_key,
            name=f"Copilot Demo ({args.jira_project_key})",
            lead_account_id=lead_account_id,
        )
        print(f"✅ Jira project created: {args.jira_project_key}")

        # Create demo issues
        issue_keys: list[str] = []
        for issue_data in DEMO_ISSUES:
            key = create_issue(
                jira_client,
                args.jira_project_key,
                issue_data["summary"],
                issue_data["description"],
            )
            issue_keys.append(key)
            print(f"   Created {key}: {issue_data['summary']}")
    finally:
        jira_client.close()

    # --- Output configuration ---
    print_config_output(
        gitlab_url=gitlab_url,
        gitlab_project_url=project.web_url,
        gitlab_project_path=project.path_with_namespace,
        jira_url=jira_url,
        jira_project_key=args.jira_project_key,
        jira_issue_keys=issue_keys,
        webhook_secret=webhook_secret,
        webhook_url=webhook_url,
        webhook_configured=webhook_configured,
    )


if __name__ == "__main__":
    main()
