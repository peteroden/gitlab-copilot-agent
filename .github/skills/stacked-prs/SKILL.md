---
name: stacked-prs
description: Procedure for creating and managing stacked PRs when a task exceeds 200 diff lines. Use this when a task is too large for a single PR.
---

When a task will produce more than 200 diff lines, split it into a stack of PRs. Each PR must be fully functional, standalone, and independently reviewable.

## When to Stack

- Estimated diff > 200 lines after scoping.
- Task has natural layers (e.g., data model → API → UI).
- Multiple independent concerns in one task.

## Procedure

### 1. Plan the Stack

Before writing code, define the stack:

```
Stack for feature/PROJ-123-user-auth:
  PR 1: feature/PROJ-123-user-auth-model    — User model and migrations
  PR 2: feature/PROJ-123-user-auth-api      — Auth API endpoints
  PR 3: feature/PROJ-123-user-auth-ui       — Login/signup UI
```

Each PR must:
- Be fully functional on its own (tests pass, no broken state).
- Provide standalone value (not just a partial step).
- Be ≤200 diff lines.

### 2. Create Branches

```bash
# PR 1: branch from main
git checkout -b feature/PROJ-123-user-auth-model main

# PR 2: branch from PR 1
git checkout -b feature/PROJ-123-user-auth-api feature/PROJ-123-user-auth-model

# PR 3: branch from PR 2
git checkout -b feature/PROJ-123-user-auth-ui feature/PROJ-123-user-auth-api
```

### 3. Open PRs

- PR 1 targets `main`.
- PR 2 targets PR 1's branch.
- PR 3 targets PR 2's branch.
- Note the stack in each PR description:
  ```
  ## Stack
  - **PR 1** (this): User model and migrations → `main`
  - PR 2: Auth API endpoints → `feature/PROJ-123-user-auth-model`
  - PR 3: Login/signup UI → `feature/PROJ-123-user-auth-api`
  ```

### 4. Merge Order

Always bottom-up:
1. Merge PR 1 into `main`.
2. Rebase PR 2 onto `main`, then merge.
3. Rebase PR 3 onto `main`, then merge.

### 5. Handle Changes

If a reviewer requests changes to PR 1:
1. Make changes on PR 1's branch.
2. Rebase PR 2 onto updated PR 1.
3. Rebase PR 3 onto updated PR 2.
4. Force-push the rebased branches.
