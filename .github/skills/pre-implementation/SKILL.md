---
name: pre-implementation
description: Mandatory checklist before starting implementation. Use this before writing code for any non-trivial task.
---

Run through this checklist before writing any code. Skip a step only if it genuinely does not apply (e.g., no UI = skip designer).

## 1. Requirements Validated

Invoke `@product` to confirm:
- [ ] The task has a clear problem statement
- [ ] Acceptance criteria are defined and testable
- [ ] Scope boundaries are explicit (what's in, what's out)

If the repo is on GitHub, a GitHub Issue must exist with acceptance criteria before proceeding.

## 2. Design Reviewed (if applicable)

Invoke `@architect` if the task involves:
- New services, APIs, or system boundaries
- Technology choices or new dependencies
- Data model changes
- Non-trivial integration patterns

Confirm:
- [ ] Approach is sound and consistent with existing architecture
- [ ] Trade-offs are documented (ADR if decision is hard to reverse)

## 3. UX Reviewed (if applicable)

Invoke `@designer` if the task involves:
- User-facing UI or interaction changes
- Developer experience changes (CLI, config, error messages)
- New workflows that humans will interact with

Confirm:
- [ ] Interaction patterns are defined
- [ ] Accessibility requirements are addressed

## 4. Work Tracked

- [ ] GitHub Issue exists with acceptance criteria (or local tracking if repo is unpublished)
- [ ] Branch created from the issue: `<type>/<issue-number>-<short-description>`

## 5. Implementation Plan

- [ ] Task is ≤200 diff lines (split into stacked PRs if larger)
- [ ] Devcontainer is running and commands will execute inside it

## When to Skip

- **Single-line fixes** (typos, config tweaks): skip entirely.
- **Documentation-only changes** (README updates, comment fixes): skip steps 2 and 3. Note: new skills, instructions, or agent files are NOT documentation-only — they define process and require product/architecture review.
- **Exploratory/spike work**: skip, but re-run before converting to a real PR.
