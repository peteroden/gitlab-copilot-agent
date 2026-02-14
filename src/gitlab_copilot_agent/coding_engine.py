"""Copilot coding engine — implements changes from a Jira issue."""

from gitlab_copilot_agent.config import Settings
from gitlab_copilot_agent.copilot_session import run_copilot_session

SYSTEM_PROMPT = """\
You are a senior software engineer implementing changes described in a Jira issue.

Your workflow:
1. Read the issue description carefully to understand requirements
2. Explore the existing codebase using file tools to understand structure and conventions
3. Make minimal, focused changes that address the issue
4. Follow existing project conventions (code style, patterns, architecture)
5. Run tests if available to verify your changes
6. Output a summary of changes made

Guidelines:
- Make the smallest change that solves the problem
- Preserve existing behavior unless the issue explicitly requires changes
- Follow SOLID principles and existing patterns
- Add tests for new functionality
- Update documentation if needed
- Do not introduce new dependencies without strong justification

Output format:
Provide a summary of:
- Files modified or created
- Key changes made
- Test results (if tests were run)
- Any concerns or follow-up items
"""


def build_coding_prompt(
    issue_key: str,
    summary: str,
    description: str | None,
) -> str:
    """Build the user prompt for a coding task."""
    desc_text = description if description else "(no description provided)"
    return (
        f"## Jira Issue: {issue_key}\n"
        f"**Summary:** {summary}\n"
        f"**Description:**\n{desc_text}\n\n"
        f"Implement the changes described in this issue. "
        f"Explore the repository, make necessary changes, run tests, "
        f"and provide a summary of what you did."
    )


async def run_coding_task(
    settings: Settings,
    repo_path: str,
    issue_key: str,
    summary: str,
    description: str | None,
) -> str:
    """Run a Copilot agent session to implement changes from a Jira issue."""
    return await run_copilot_session(
        settings=settings,
        repo_path=repo_path,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_coding_prompt(issue_key, summary, description),
    )
