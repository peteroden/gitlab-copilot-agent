# Copilot Instructions — Shared Baseline

These instructions apply to all agents regardless of role.

## Environment

**All dev tool execution happens inside the devcontainer. No exceptions.**

See `.github/instructions/devcontainer.instructions.md` for context detection, command rules, and failure recovery.

## Pre-Implementation Gate

**STOP. Before implementing any non-trivial task, invoke the `pre-implementation` skill and complete all applicable steps.** Do not write code, create files, or make changes until requirements are validated with `@product` and design is reviewed with `@architect` and `@designer` (if applicable).

Skip only for single-line fixes, typos, or config tweaks.

## Quality Standard

No slop. All output — code, documents, plans, commits, PRs — must be small, well-written, succinct, and easy to understand. The bar: would a busy senior engineer want to read this? If not, rewrite it shorter.

- **Code**: No boilerplate for boilerplate's sake. No wrapper functions that add nothing. No comments that restate the code.
- **Documents**: No filler paragraphs. Say it once, say it clearly, move on.
- **Plans**: Concrete actions, not vague intentions.
- **Commits/PRs**: Every word carries information. No padding.
- **Tests**: Test meaningful behavior, not implementation details.

## Assumption Surfacing

Before acting on non-trivial assumptions, state them explicitly:

```
ASSUMPTIONS:
1. [assumption]
2. [assumption]
→ Proceeding with these unless corrected.
```

Interactive agents: surface assumptions and wait for confirmation.
Yolo agents: document assumptions in commit messages and PR descriptions.

## Work Tracking

- Once a repo is published to GitHub, use **GitHub Issues** to track all planned work.
- Every task should have an issue with acceptance criteria before work begins.
- PRs must reference their issue (`Closes #N` in the PR description).
- Branch names use the issue number: `<type>/<issue-number>-<short-description>`.
- Local tracking (session SQL todos) is acceptable for unpublished repos or exploratory work.
- See the `github-workflow` skill for detailed procedures.

## Conventions

- American English in all code, comments, and documentation.
- Conventional Commits: `<type>(<scope>): <summary>`
- Branch naming: `<type>/<ticket-id>-<short-description>`
- No secrets in source code.
- No hardcoded values — use config with reasonable defaults.
- Prefer the standard library. Justify every new dependency.
- Pin all dependency versions.
- Fail fast — surface errors immediately with clear messages.

## Hard Rules

These are non-negotiable. Violating any of these is a session failure.

1. **NEVER merge PRs.** Create the PR, then stop. The user reviews and merges all PRs. No exceptions, no "it's a small change", no "CI passed so it's safe."
2. **Run the full CI-equivalent locally before every commit.** If the project has a `make lint`, `make test`, or equivalent — run it. Do not run individual tools (e.g., just the formatter) as a shortcut. If CI fails on something you could have caught locally, that's a bug in your process.
3. **Cross-model code review before every PR push.** No exceptions for "small" PRs. Invoke the `code-review` skill or a code-review agent using a different model than the one that wrote the code.
4. **Verify your git branch before every commit.** Run `git branch --show-current` before committing, especially after rebase, stash, or checkout operations. Committing to the wrong branch wastes time and risks force-push accidents.
5. **Smoke-test behavioral changes.** Unit tests passing is necessary but not sufficient. If you change logging, telemetry, startup behavior, or error handling — verify with a quick manual run if possible. Don't let the user discover broken behavior live.

## PR Requirements

- ≤200 diff lines (additions + deletions). If larger, split into stacked PRs.
- Each stacked PR must be fully functional and standalone.
- **Code review before push**: invoke a code review agent (ideally a different model than the one that wrote the code) on every PR. Fix all High/Critical findings before merge. Create follow-up issues for Medium findings.
- PR description format:
  ```
  ## What
  <Brief description>

  ## Why
  <Motivation, issue/ticket reference>

  ## How to Test
  <Verification steps>
  ```

## Security

- Review all code against OWASP Top 10 before submitting.
- Flag code touching auth, authorization, data storage, or external communication for human review.
- Validate and sanitize all inputs.
- Use parameterized queries.
- Least privilege in code and process.

## Testing

- Code is incomplete without tests.
- Test our code, not third-party libraries. Mock at the boundary.
- Tests written alongside code, not after.
- All tests must pass before PR merge.

## Agent Delegation

When planning or executing non-trivial work, delegate to the appropriate agent role:

| Phase | Agent | Use for |
|-------|-------|---------|
| Planning | `@product` | Defining requirements, user stories, acceptance criteria, scoping |
| Planning | `@architect` | Evaluating system design, technology choices, API contracts, ADRs |
| Planning | `@designer` | UX/UI patterns, interaction flows, accessibility (for end users and developer experience) |
| Execution | `@developer` | Writing code, tests, and documentation |
| Execution | `@orchestrator` | Task breakdown, PR sequencing, worktree management |

Don't do everything yourself — involve the right agent at the right phase.

## References

- See `GOVERNANCE.md` for the full governance model.
- See `.github/agents/` for role-specific custom agent profiles.
