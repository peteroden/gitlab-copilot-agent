# Copilot Instructions — Shared Baseline

These instructions apply to all agents regardless of role.

## Environment

- Execute all commands inside the devcontainer: `devcontainer exec --workspace-folder . <command>`
- Start the container if needed: `devcontainer up --workspace-folder .`
- Never install development dependencies directly on the host.

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

## Conventions

- American English in all code, comments, and documentation.
- Conventional Commits: `<type>(<scope>): <summary>`
- Branch naming: `<type>/<ticket-id>-<short-description>`
- No secrets in source code.
- No hardcoded values — use config with reasonable defaults.
- Prefer the standard library. Justify every new dependency.
- Pin all dependency versions.
- Fail fast — surface errors immediately with clear messages.

## PR Requirements

- ≤200 diff lines (additions + deletions). If larger, split into stacked PRs.
- Each stacked PR must be fully functional and standalone.
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

## References

- See `GOVERNANCE.md` for the full governance model.
- See `.github/agents/` for role-specific custom agent profiles.
