---
name: github-workflow
description: Procedures for publishing repos, creating issues, managing project boards, and linking PRs. Use this when a project is published to GitHub or when planning tracked work.
---

## When to Use GitHub Issues vs Local Tracking

| Context | Tracking method |
|---------|----------------|
| Repo not yet on GitHub | Local tracking (session SQL todos) |
| Exploratory work / spikes | Local tracking |
| Repo is on GitHub AND work has defined scope | GitHub Issues |
| Any work with acceptance criteria | GitHub Issues |

Once a repo is published, default to GitHub Issues for all planned work.

## Initial Publish

Squash all local commits into a single commit before pushing to GitHub for the first time:

```bash
# From the repo root, squash everything into one commit
git reset --soft $(git rev-list --max-parents=0 HEAD)
git commit -m "feat: initial commit"

# Create the remote and push
gh repo create <owner>/<repo> --source . --push
```

This gives the GitHub repo a clean single-commit history.

## Issue Creation

Every planned task gets a GitHub Issue:

```bash
gh issue create --title "<type>(<scope>): <summary>" \
  --body "## What
<description>

## Acceptance Criteria
- [ ] <criterion 1>
- [ ] <criterion 2>

## Out of Scope
<what's excluded>" \
  --label "<type>"
```

Rules:
- One issue per task. If a task needs splitting, create multiple issues.
- Acceptance criteria must be testable — "it works" is not a criterion.
- Product agent creates issues; orchestrator assigns and sequences them.

## Labels

Apply labels when creating or triaging issues:

| Category | Labels | Applied by |
|----------|--------|------------|
| Type | `feat`, `fix`, `docs`, `refactor`, `test`, `chore` | Product (at creation) |
| Priority | `p0-critical`, `p1-high`, `p2-medium`, `p3-low` | Product (at creation) |
| Status | `blocked`, `needs-design`, `needs-arch` | Orchestrator (during triage) |

## Epics and Sub-Issues

When a feature is too large for a single issue (even after scoping):

1. Product creates a **parent issue** labeled `epic` with the overall goal and acceptance criteria.
2. Product or Orchestrator breaks it into **child issues**, each referencing the parent: `Part of #<parent>`.
3. Parent issue includes a checklist of children: `- [ ] #<child>`.
4. Only child issues are assigned to developers. The parent tracks overall progress.

## Stacked PRs and Issues

When work requires stacked PRs (>200 diff lines):

- Each stacked PR gets its **own issue** with its own acceptance criteria.
- Issues reference the parent epic if one exists.
- Each PR's description includes `Closes #<its-issue>` and notes its position in the stack.

## Branch Naming

Use the issue number as the ticket ID:

```
<type>/<issue-number>-<short-description>
```

Examples: `feature/42-add-oauth`, `fix/87-null-pointer-in-parser`

## PR Linking

Every PR must reference its issue in the description:

```
Closes #42
```

This auto-closes the issue when the PR merges.

## GitHub Projects Board

Workflow columns:

| Column | Meaning |
|--------|---------|
| Backlog | Issue created, not yet started |
| In Progress | Work has begun (branch created, agent assigned) |
| In Review | PR opened, awaiting review |
| Done | PR merged, acceptance criteria verified |

### Transition Rules

| Transition | Triggered by | Who |
|------------|-------------|-----|
| → Backlog | Issue created | Product |
| Backlog → In Progress | Task assigned, branch created | Orchestrator |
| In Progress → In Review | PR opened | Developer |
| In Review → Done | PR merged + Product verifies acceptance criteria | Product / auto via `Closes #N` |

### Board Health Check

Orchestrator periodically verifies board state matches reality:
- All merged PRs have issues in Done
- All open PRs have issues in In Review
- Stale In Progress issues (no commits in 24h) are flagged

## Ownership

| Role | Responsibility |
|------|---------------|
| Product | Create issues with acceptance criteria, apply type/priority labels, verify acceptance criteria before closure |
| Orchestrator | Manage board, assign issues, apply status labels, sequence work, ensure PR-issue linking, board health checks |
| Developer | Work the issue, open PR with `Closes #N`, move to In Review |
