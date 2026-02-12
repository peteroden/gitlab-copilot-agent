---
name: conventional-commits
description: Reference for Conventional Commits format and branch naming. Use this when writing commit messages or creating branches.
---

## Commit Format

```
<type>(<scope>): <short summary>

<optional body: what and why>

<optional footer: breaking changes, issue refs>
```

- Summary line ≤72 characters.
- Body explains *what* and *why*, not *how*.
- Footer for breaking changes and issue references.

## Types

| Type | Use for |
|------|---------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `style` | Formatting, whitespace (no logic change) |
| `refactor` | Code restructuring (no behavior change) |
| `test` | Adding or updating tests |
| `chore` | Maintenance (deps, config, tooling) |
| `ci` | CI/CD pipeline changes |
| `perf` | Performance improvement |
| `build` | Build system changes |

## Examples

```
feat(auth): add OAuth2 PKCE flow

Implements the PKCE extension for the OAuth2 authorization code flow
to support public clients (SPA, mobile).

Closes #123
```

```
fix(api): prevent SQL injection in user query

User-supplied sort parameter was concatenated directly into the query.
Now uses parameterized queries.
```

```
refactor(payments): extract billing calculator

Moves billing logic from the order handler into a dedicated module
to improve testability and reuse.
```

## Breaking Changes

Add `!` after the type and a `BREAKING CHANGE` footer:

```
feat(api)!: change user endpoint response format

BREAKING CHANGE: The /users endpoint now returns a paginated response
instead of an array. Clients must update to handle the new format.
```

## Branch Naming

Format: `<type>/<ticket-id>-<short-description>`

| Type | Use for |
|------|---------|
| `feature/` | New features |
| `fix/` | Bug fixes |
| `chore/` | Maintenance |
| `docs/` | Documentation |
| `refactor/` | Code restructuring |
| `test/` | Test additions |

Examples:
- `feature/PROJ-123-add-oauth-login`
- `fix/PROJ-456-null-pointer-in-parser`
- `refactor/PROJ-789-extract-payment-service`
