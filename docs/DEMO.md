# Demo Environment Setup

Automated provisioning of a demo environment for showcasing the GitLab Copilot Agent.

## Prerequisites

- Python 3.11+ with `uv`
- GitLab personal access token with `api` scope
- Jira API token with **Administer Jira** global permission
- (Optional) [ngrok](https://ngrok.com) for webhook tunneling

## Quick Start

```bash
# 1. Set credentials
export GITLAB_URL=https://gitlab.com
export GITLAB_TOKEN=glpat-...
export JIRA_URL=https://yourco.atlassian.net
export JIRA_EMAIL=you@company.com
export JIRA_API_TOKEN=...

# 2. (Optional) Start ngrok for webhook auto-detection
ngrok http 8000

# 3. Provision demo environment
uv run scripts/demo_provision.py \
  --gitlab-group myorg \
  --jira-project-key DEMO
```

The script will:
- Create a private GitLab project with a demo blog API (intentional issues included)
- Create a Jira project with 3 demo stories
- Create the **"AI Ready"** and **"In Review"** workflow statuses on the Jira board (workflow: To Do → AI Ready → In Progress → In Review → Done)
- Auto-detect ngrok and configure the GitLab webhook (if running)
- Output the `JIRA_PROJECT_MAP` configuration and next steps

## CLI Reference

```
usage: demo_provision.py [-h] --gitlab-group GROUP --jira-project-key KEY
                         [--gitlab-project-name NAME] [--webhook-url URL]
                         [--trigger-status STATUS]

Options:
  --gitlab-group          GitLab group/namespace (required)
  --jira-project-key      Jira project key, e.g. DEMO (required)
  --gitlab-project-name   Project name (default: copilot-demo)
  --webhook-url           Agent URL for webhook setup; auto-detects ngrok if omitted
  --trigger-status        Jira status that triggers the agent (default: "AI Ready")
```

## Demo Walkthrough (15-20 minutes)

### 1. Show the provisioned environment (2 min)

- Open the GitLab project in a browser
- Browse the code — it's a realistic FastAPI blog API
- Point out: "This has intentional issues the agent will find"

### 2. Trigger the Jira → GitLab flow (5 min)

- Open Jira, show the 3 demo stories
- Pick **DEMO-1** ("Add user authentication endpoint")
- Move it to **"AI Ready"**
- Switch to GitLab — watch the agent create a branch and MR
- Narrate: "The agent read the Jira story and is now implementing it"

### 3. Show the MR review (5 min)

- Open the MR the agent created (or create one manually for the existing demo code)
- Show the inline review comments with severity tags
- Show the **apply-able code suggestions** — click "Apply suggestion" to demo one-click fixes
- Narrate: "Every comment includes a concrete fix you can apply with one click"

### 4. Repo config discovery — the key differentiator (3 min)

- Open `.github/copilot-instructions.md` in the GitLab UI
- Narrate: "These instructions get appended to the agent's system prompt"
- Show a review comment that enforces a rule from the instructions (e.g., parameterized queries)
- Open `.github/skills/security-patterns/SKILL.md`
- Narrate: "Skills teach the agent YOUR team's approved patterns"
- Open `.github/agents/security-reviewer.agent.md`
- Narrate: "Custom agents add specialized reviewers — this one focuses on security"
- **Key message:** "The agent adapts to YOUR codebase. It's not generic AI — it's YOUR AI reviewer."

### 5. Demo the `/copilot` command (3 min)

- On any open MR, add a comment: `/copilot fix the type hints in models.py`
- Watch the agent push a commit with the changes
- Narrate: "Developers can direct the agent with natural language on any MR"

## Demo Code — What's Inside

The demo repository is a FastAPI blog post API with these intentional issues:

| Issue | File | What the agent should find |
|-------|------|---------------------------|
| SQL injection | `database.py` | String interpolation in SQL queries |
| Hardcoded secret | `auth.py` | `API_KEY = "sk_demo_not_real..."` |
| Missing type hints | `models.py` | `PostList.posts: list` without type param |
| No error handling | `main.py` | Bare `except` and `print()` for errors |
| Missing docstrings | Multiple files | Public functions without docstrings |
| `print()` instead of logging | `main.py`, `auth.py` | Uses `print()` instead of `logging` |

### Repo Configuration Files

| File | Type | Purpose |
|------|------|---------|
| `.github/copilot-instructions.md` | Instructions | Project-wide review standards |
| `.github/instructions/python.instructions.md` | Instructions | Python-specific conventions |
| `AGENTS.md` | Instructions | Root-level behavioral guidance |
| `.github/skills/security-patterns/SKILL.md` | Skill | Approved/forbidden security patterns |
| `.github/agents/security-reviewer.agent.md` | Agent | Custom security review specialist |

## Automated Video Recording

Record a 2-3 minute demo video using Playwright. The script connects to your existing browser session (preserving logins), records all interactions, and uses an embedded OTEL collector to detect when the agent completes operations — no arbitrary sleeps.

### Prerequisites

1. **Install Playwright** (one-time):
   ```bash
   uv pip install 'playwright>=1.48.0'
   ```
   No need to run `playwright install` — the script connects to your existing browser via CDP.

2. **Launch your Chromium-based browser with remote debugging**:
   ```bash
   # Edge (macOS)
   /Applications/Microsoft\ Edge.app/Contents/MacOS/Microsoft\ Edge --remote-debugging-port=9222

   # Chrome (macOS)
   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

   # Linux (either)
   microsoft-edge --remote-debugging-port=9222
   google-chrome --remote-debugging-port=9222
   ```

3. **Log into GitLab and Jira** in that Chrome window.

4. **Start the agent** with OTEL export pointed at the recording script's collector:
   ```bash
   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 uv run uvicorn ...
   ```

5. **Ensure the demo environment is provisioned** (see Quick Start above).

### Recording

```bash
uv run python scripts/demo_record.py \
  --gitlab-mr-url https://gitlab.com/myorg/copilot-demo/-/merge_requests/1 \
  --jira-board-url https://myorg.atlassian.net/jira/software/projects/DEMO/boards/42
```

The script runs three scenes:

| Scene | What happens | Transition trigger |
|-------|-------------|-------------------|
| 1. Code Review | Navigates to MR, scrolls through review comments | `review_complete` OTEL event |
| 2. /copilot Command | Types `/copilot fix ...` on the MR, shows commit + diff | `copilot_command_complete` event |
| 3. Jira Flow | Shows Jira board → agent codes → MR created → "In Review" | `coding_task_complete` event |

### Options

```
--otel-port PORT     OTEL collector port (default: 4317)
--cdp-url URL        Chrome DevTools URL (default: http://localhost:9222)
--output-dir DIR     Video output directory (default: demo-video/)
--skip-scene N       Skip scene N (1, 2, or 3). Repeatable.
--gitlab-base-url    Override auto-detected GitLab base URL
--project-path       Override auto-detected project path
```

### Examples

```bash
# Record only Scene 1 (code review)
uv run python scripts/demo_record.py \
  --gitlab-mr-url https://gitlab.com/myorg/copilot-demo/-/merge_requests/1 \
  --jira-board-url https://myorg.atlassian.net/jira/software/projects/DEMO/boards/42 \
  --skip-scene 2 --skip-scene 3

# Use a different OTEL port
uv run python scripts/demo_record.py \
  --otel-port 4318 \
  --gitlab-mr-url ... \
  --jira-board-url ...
```

### Output

Video is saved as `.webm` in `demo-video/`. Convert to MP4:

```bash
ffmpeg -i demo-video/video.webm -c:v libx264 demo-video/demo.mp4
```

### How It Works

The script embeds a lightweight OTEL collector (same pattern as `scripts/otel_console_collector.py`) that listens for agent log events via gRPC. When the agent emits `review_complete`, `copilot_command_complete`, etc., the script refreshes the page and transitions to the next scene. If an event doesn't arrive within the timeout, it falls back to page refresh + visual detection.

## Cleanup

The script prints cleanup URLs at the end. To remove demo resources manually:

1. **GitLab:** Project → Settings → General → Advanced → Delete project
2. **Jira:** Project Settings → Delete project (or archive)

## Troubleshooting

**"GitLab group not found"**
- Check `--gitlab-group` matches an existing group you have access to
- Use the full path for nested groups: `myorg/subgroup`

**"Jira project already exists"**
- Delete the existing project or use a different `--jira-project-key`

**"401 Unauthorized" on Jira**
- Verify `JIRA_EMAIL` and `JIRA_API_TOKEN` are correct
- For project creation, the account needs **Administer Jira** global permission

**Webhook not triggering**
- Check ngrok is running: `curl http://127.0.0.1:4040/api/tunnels`
- Verify the webhook in GitLab → Settings → Webhooks → Recent Deliveries
- Ensure the agent service is running on port 8000
