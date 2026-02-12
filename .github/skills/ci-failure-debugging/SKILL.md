---
name: ci-failure-debugging
description: Guide for debugging failing CI/CD workflows. Use this when CI checks fail on a PR or when asked to debug build/test failures.
---

When CI fails on a PR, follow this process to diagnose and fix the issue.

## Step 1: Identify the Failure

```bash
# List recent workflow runs for the PR
gh run list --branch <branch-name> --limit 5
```

Or use the GitHub MCP Server:
- `list_workflow_runs` to find the failed run.
- `list_workflow_jobs` to find the failed job.

## Step 2: Get the Logs

```bash
# View logs for a specific run
gh run view <run-id> --log-failed
```

Or use the GitHub MCP Server:
- `get_job_logs` with `return_content: true` for the failed job.
- Use `tail_lines: 100` to get the relevant end of the log.

## Step 3: Categorize the Failure

| Category | Symptoms | Action |
|----------|----------|--------|
| **Build failure** | Compilation errors, type errors | Fix the code |
| **Test failure** | Assertion errors, test timeouts | Fix the test or the code |
| **Lint failure** | Style violations, warnings-as-errors | Run formatter/linter locally, fix |
| **Dependency failure** | Package not found, version conflict | Check lockfile, pin versions |
| **Infra failure** | Network timeout, runner issue | Retry the run |
| **PR size check** | Diff too large | Split into stacked PRs |

## Step 4: Reproduce Locally

```bash
# Run the same command that failed in CI, inside the devcontainer
devcontainer exec --workspace-folder . <failed-command>
```

## Step 5: Fix and Verify

1. Fix the issue.
2. Run the failing command locally to confirm the fix.
3. Commit with a clear message: `fix(ci): resolve <what was broken>`.
4. Push and verify CI passes.

## Common Pitfalls

- **Flaky tests**: If a test passes locally but fails in CI, check for timing dependencies, environment assumptions, or non-deterministic behavior.
- **Different environments**: CI may use a different OS, Node version, or Python version. Check the workflow file.
- **Cached state**: CI runs from clean state. If it works locally, check for uncommitted files or local-only config.
