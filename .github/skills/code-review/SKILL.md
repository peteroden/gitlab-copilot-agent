---
name: code-review
description: Cross-model code review before every PR. Use this after code is written and before pushing.
---

## When to Review

Every PR gets a code review before push. No exceptions.

## How to Review

1. Use a **different model** than the one that wrote the code. Cross-model review consistently catches bugs that self-review misses.
2. Review the branch diff against the base branch, not individual files.
3. Focus on: bugs, security issues, logic errors, missing edge cases. Ignore style and formatting.

## Handling Findings

| Severity | Action |
|----------|--------|
| **Critical/High** | Fix before merge. No exceptions. |
| **Medium** | Fix if within scope. Otherwise create a follow-up issue. |
| **Low/Info** | Note in PR description. Fix if trivial. |

## What Good Reviews Catch

Real examples from production sessions:

- **Idempotency bugs**: init function leaks threads when called twice
- **Env var leakage**: `DOCKER_HOST` forwarded to all sandbox methods, enabling container escape from bwrap
- **Missing validation**: config allows invalid state (e.g., DinD without shared volume)
- **Drift bugs**: counter incremented before the thing it counts actually happens
- **Uncounted paths**: error handling that skips metric recording

## Anti-Patterns

- Reviewing your own code with the same model that wrote it (blind spots are shared)
- Skipping review for "small" changes (small changes cause big outages)
- Treating all findings as blocking (Medium findings can be follow-up issues)
- Reviewing style instead of substance (formatters handle style)

## Pre-Review Gate

Before reviewing logic, verify the author ran both linter and formatter:

1. Check the diff for formatting-only changes (inconsistent whitespace, import ordering). If present, reject — the author didn't run the formatter.
2. If you can run commands, execute the project's lint and format-check commands on the changed files. Report any failures as **High** severity (broken CI pipeline).
3. Only proceed to logic review after lint/format is clean.
