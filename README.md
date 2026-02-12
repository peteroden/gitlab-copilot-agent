# gitlab-copilot-agent

GitLab Copilot Agent.

## What This Is

A project bootstrapped with agent-driven development governance:
- **Governance model** — rules for how AI agents operate on this codebase.
- **Agent instruction files** — role-specific instructions for Product, Architect, Designer, Orchestrator, and Developer agents.
- **CI checks** — automated PR size enforcement.

## Structure

```
.github/
├── copilot-instructions.md              # Shared baseline for all agents
├── agents/
│   ├── developer.agent.md               # Developer agent (yolo, restricted tools)
│   ├── architect.agent.md               # Architect agent (interactive)
│   ├── designer.agent.md                # Designer agent (interactive)
│   ├── product.agent.md                 # Product agent (interactive)
│   └── orchestrator.agent.md            # Orchestrator agent (interactive)
├── skills/
│   ├── owasp-review/SKILL.md            # OWASP Top 10 security review checklist
│   ├── stacked-prs/SKILL.md             # Procedure for stacked PRs
│   ├── worktree-setup/SKILL.md          # Worktree + devcontainer setup
│   ├── devcontainer-setup/SKILL.md      # Devcontainer templates (yolo/interactive)
│   ├── adr-creation/SKILL.md            # Architecture Decision Record template
│   ├── conventional-commits/SKILL.md    # Commit and branch naming reference
│   └── ci-failure-debugging/SKILL.md    # CI failure diagnosis steps
└── workflows/
    └── pr-size-check.yml                # Enforce ≤200 diff line PRs
GOVERNANCE.md                            # Full governance document
```

## Key Rules

- All coding in devcontainers.
- 1 task = 1 agent = 1 worktree = 1 devcontainer.
- PRs ≤200 diff lines. Stacked PRs if larger.
- Conventional commits. Conventional branches.
- OWASP self-review on every PR.
- No slop.

See `GOVERNANCE.md` for full details.
