---
name: worktree-setup
description: Procedure for creating a git worktree and devcontainer for a new task. Use this when starting a new development task.
---

Each task gets its own worktree and devcontainer instance. This ensures isolation — the worst case is rolling back a branch.

## Create a Worktree

```bash
# From the main repo directory
git worktree add ../worktrees/<branch-name> -b <type>/<ticket-id>-<short-description>
```

Branch naming follows conventional format:
- `feature/PROJ-123-add-oauth-login`
- `fix/PROJ-456-null-pointer-in-parser`
- `refactor/PROJ-789-extract-payment-service`

## Start the Devcontainer

For a **yolo (developer) agent** — no network:
```bash
devcontainer up --workspace-folder ../worktrees/<branch-name>
```
Ensure the devcontainer.json uses `"network": "none"` for yolo agents.

For an **interactive agent** — with network:
```bash
devcontainer up --workspace-folder ../worktrees/<branch-name>
```

## Run Commands Inside

```bash
devcontainer exec --workspace-folder ../worktrees/<branch-name> <command>
```

## Verify the Environment

```bash
# Check the branch
devcontainer exec --workspace-folder ../worktrees/<branch-name> git branch --show-current

# Check tools are available
devcontainer exec --workspace-folder ../worktrees/<branch-name> <build-command>
```

## Clean Up After Merge

```bash
# Stop the devcontainer (find container ID first)
docker ps --filter "label=devcontainer.local_folder=../worktrees/<branch-name>" -q | xargs docker stop

# Remove the worktree
git worktree remove ../worktrees/<branch-name>

# Delete the branch if merged
git branch -d <branch-name>
```

## Notes

- One worktree = one task = one agent = one devcontainer.
- Worktrees share the same repo objects but have independent working directories.
- Multiple worktrees can run concurrently — monitor machine resources.
