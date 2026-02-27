#!/usr/bin/env python3
"""Demo environment provisioner for GitLab Copilot Agent.

Creates a GitLab project with demo code and a Jira project with demo stories,
then outputs the configuration needed to connect them to the agent service.

Usage:
    uv run scripts/demo_provision.py \\
        --gitlab-group myorg \\
        --jira-project-key DEMO \\
        --gitlab-url https://gitlab.com \\
        --gitlab-token glpat-xxx \\
        --jira-url https://myorg.atlassian.net \\
        --jira-email user@example.com \\
        --jira-api-token xxx

Credentials can also be provided via environment variables:
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
from demo_provision.gitlab_provisioner import (
    create_merge_request,
    create_webhook,
    get_namespace,
    load_template,
    push_files,
)
from demo_provision.gitlab_provisioner import (  # noqa: E402
    create_project as gl_create_project,
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
    create_statuses as jira_create_statuses,
)
from demo_provision.jira_provisioner import (
    get_project as jira_get_project,
)

TEMPLATE_DIR = Path(__file__).parent / "demo_templates" / "blog-api"


def _require_value(cli_value: str | None, env_name: str) -> str:
    """Get a value from CLI arg (preferred) or environment variable, or exit."""
    value = (cli_value or "").strip() or os.environ.get(env_name, "").strip()
    if not value:
        print(
            f"Error: --{env_name.lower().replace('_', '-')} or {env_name} env var is required.",
            file=sys.stderr,
        )
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
    parser.add_argument(
        "--use-existing-gitlab-project",
        action="store_true",
        help="Use an existing GitLab project instead of creating a new one.",
    )
    parser.add_argument(
        "--use-existing-jira-project",
        action="store_true",
        help="Use an existing Jira project instead of creating a new one.",
    )
    parser.add_argument(
        "--gitlab-url", default=None, help="GitLab instance URL (or GITLAB_URL env)"
    )
    parser.add_argument(
        "--gitlab-token", default=None, help="GitLab API token (or GITLAB_TOKEN env)"
    )
    parser.add_argument("--jira-url", default=None, help="Jira instance URL (or JIRA_URL env)")
    parser.add_argument("--jira-email", default=None, help="Jira user email (or JIRA_EMAIL env)")
    parser.add_argument(
        "--jira-api-token", default=None, help="Jira API token (or JIRA_API_TOKEN env)"
    )
    args = parser.parse_args()

    # Gather credentials (CLI flags take precedence over env vars)
    gitlab_url = _require_value(args.gitlab_url, "GITLAB_URL")
    gitlab_token = _require_value(args.gitlab_token, "GITLAB_TOKEN")
    jira_url = _require_value(args.jira_url, "JIRA_URL")
    jira_email = _require_value(args.jira_email, "JIRA_EMAIL")
    jira_api_token = _require_value(args.jira_api_token, "JIRA_API_TOKEN")

    # --- GitLab provisioning ---
    import gitlab

    gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)
    gl.auth()

    project_path = f"{args.gitlab_group}/{args.gitlab_project_name}"
    existing = gl_get_project(gl, project_path)
    if existing and not args.use_existing_gitlab_project:
        print(
            f"Error: GitLab project '{project_path}' already exists.\n"
            f"Delete it, use a different --gitlab-project-name, "
            f"or pass --use-existing-gitlab-project.",
            file=sys.stderr,
        )
        sys.exit(1)

    if existing and args.use_existing_gitlab_project:
        project = existing
        print(f"✅ Using existing GitLab project: {project.web_url}")
    else:
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

    # --- Create demo MR for agent to review ---
    demo_mr = None
    try:
        demo_mr = create_merge_request(
            project,
            source_branch="feature/add-search-endpoint",
            target_branch="main",
            title="Add post search endpoint",
            description=(
                "Adds a search endpoint to find posts by keyword.\n\n"
                "This MR has intentional issues for the agent to review."
            ),
            files={
                "src/demo_app/search.py": (
                    '"""Search functionality for the Blog Post API."""\n'
                    "\n"
                    "from demo_app.database import _get_connection\n"
                    "\n"
                    "\n"
                    "def search_posts(query):\n"
                    "    conn = _get_connection()\n"
                    "    results = conn.execute(\n"
                    "        f\"SELECT * FROM posts WHERE title LIKE '%{query}%'\""
                    "\n"
                    "        f\" OR content LIKE '%{query}%'\"\n"
                    "    ).fetchall()\n"
                    "    conn.close()\n"
                    "    return [dict(row) for row in results]\n"
                    "\n"
                    "\n"
                    "def search_by_date(start, end):\n"
                    "    conn = _get_connection()\n"
                    "    results = conn.execute(\n"
                    '        f"SELECT * FROM posts WHERE created_at'
                    " BETWEEN '{start}' AND '{end}'\"\n"
                    "    ).fetchall()\n"
                    "    conn.close()\n"
                    "    return results\n"
                ),
                "src/demo_app/main.py": (
                    '"""Blog Post API — a demo FastAPI application."""\n'
                    "\n"
                    "import logging\n"
                    "\n"
                    "from fastapi import Depends, FastAPI, HTTPException\n"
                    "\n"
                    "from demo_app.auth import verify_api_key\n"
                    "from demo_app.database import (\n"
                    "    create_post,\n"
                    "    delete_post,\n"
                    "    get_all_posts,\n"
                    "    get_post,\n"
                    "    get_posts_by_author,\n"
                    ")\n"
                    "from demo_app.models import PostCreate, PostResponse\n"
                    "from demo_app.search import search_posts\n"
                    "\n"
                    "logger = logging.getLogger(__name__)\n"
                    "\n"
                    "app = FastAPI(title='Blog Post API', version='0.1.0')\n"
                    "\n"
                    "\n"
                    "@app.get('/health')\n"
                    "def health() -> dict[str, str]:\n"
                    "    return {'status': 'ok'}\n"
                    "\n"
                    "\n"
                    "@app.get('/posts/{post_id}', response_model=PostResponse)\n"
                    "def read_post(post_id: str,"
                    " _key: str = Depends(verify_api_key)) -> PostResponse:\n"
                    "    post = get_post(post_id)\n"
                    "    if not post:\n"
                    "        raise HTTPException(status_code=404,"
                    " detail='Post not found')\n"
                    "    return PostResponse(**post)\n"
                    "\n"
                    "\n"
                    "@app.get('/posts', response_model=list[PostResponse])\n"
                    "def list_posts(author: str = '') -> list[PostResponse]:\n"
                    "    if author:\n"
                    "        return [PostResponse(**p)"
                    " for p in get_posts_by_author(author)]\n"
                    "    return [PostResponse(**p) for p in get_all_posts()]\n"
                    "\n"
                    "\n"
                    "@app.get('/search')\n"
                    "def search(q: str):\n"
                    "    return search_posts(q)\n"
                    "\n"
                    "\n"
                    "@app.post('/posts', status_code=201,"
                    " response_model=PostResponse)\n"
                    "def new_post(\n"
                    "    post: PostCreate,"
                    " _key: str = Depends(verify_api_key)\n"
                    ") -> PostResponse:\n"
                    "    logger.info('Creating post: %s', post.title)\n"
                    "    result = create_post(post.title,"
                    " post.content, post.author)\n"
                    "    return PostResponse(**result)\n"
                    "\n"
                    "\n"
                    "@app.delete('/posts/{post_id}')\n"
                    "def remove_post(\n"
                    "    post_id: str,"
                    " _key: str = Depends(verify_api_key)\n"
                    ") -> dict[str, bool]:\n"
                    "    delete_post(post_id)\n"
                    "    return {'deleted': True}\n"
                ),
            },
            commit_message="Add search endpoint with keyword matching",
        )
        print(f"✅ Demo MR created: {demo_mr.web_url}")
    except Exception as e:
        print(f"⚠️  Could not create demo MR: {e}")

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
        if existing_jira and not args.use_existing_jira_project:
            print(
                f"Error: Jira project '{args.jira_project_key}' already exists.\n"
                f"Delete it, use a different --jira-project-key, "
                f"or pass --use-existing-jira-project.",
                file=sys.stderr,
            )
            sys.exit(1)

        issue_keys: list[str] = []
        if existing_jira and args.use_existing_jira_project:
            project_id = str(existing_jira["id"])
            print(f"✅ Using existing Jira project: {args.jira_project_key}")
        else:
            current_user = get_current_user(jira_client)
            lead_account_id = current_user["accountId"]

            jira_project_data = jira_create_project(
                jira_client,
                key=args.jira_project_key,
                name=f"Copilot Demo ({args.jira_project_key})",
                lead_account_id=lead_account_id,
            )
            project_id = str(jira_project_data["id"])
            print(f"✅ Jira project created: {args.jira_project_key}")

        # Ensure workflow statuses exist on the project board
        # Workflow: To Do → AI Ready → In Progress → In Review → Done
        created = jira_create_statuses(
            jira_client,
            [
                (args.trigger_status, "NEW"),  # To Do category
                ("In Review", "INDETERMINATE"),  # In Progress category
            ],
            project_id,
        )
        if created:
            print(f"✅ Created '{args.trigger_status}' and 'In Review' statuses on Jira board")
        else:
            print(
                "⚠️  Could not create statuses via API"
                " (team-managed projects require manual setup).\n"
                "   Add these columns in Jira UI:"
                " Board → Board settings → Columns\n"
                f"   Required columns: {args.trigger_status}, In Review"
            )

        # Create demo issues
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
        gitlab_project_id=project.id,
        jira_url=jira_url,
        jira_project_key=args.jira_project_key,
        jira_issue_keys=issue_keys,
        webhook_secret=webhook_secret,
        webhook_url=webhook_url,
        webhook_configured=webhook_configured,
        demo_mr_url=demo_mr.web_url if demo_mr else None,
    )


if __name__ == "__main__":
    main()
