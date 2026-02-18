---
applyTo: "**"
---

# Devcontainer Execution Rules

All dev tool commands MUST run inside the devcontainer. This applies to every file type and every project.

## Detecting Your Context

- **On the host** (workspace path starts with `/Users/`, `/home/`, `C:\`): prefix dev commands with `devcontainer exec --workspace-folder .`
- **Inside the devcontainer** (workspace path starts with `/workspaces/`): run commands directly — no prefix needed.

## Required Prefix (Host Context)

When running on the host, every dev tool invocation must be prefixed with:

```bash
devcontainer exec --workspace-folder . <command>
```

This includes: language runtimes (`python`, `node`, `go`), package managers (`uv`, `npm`, `pip`), linters (`ruff`, `mypy`, `eslint`), test runners (`pytest`, `jest`), build tools (`tsc`, `make`), inline scripts (`python3 << 'EOF'`), and application servers (`uv run uvicorn`, `npm start`).

## Commands That Don't Require the Devcontainer

These can run in either context: `git`, `gh`, `devcontainer`, `docker`, `ls`, `cat`, `mkdir`, `cp`, `mv`, `find`, `grep`.

## Failure Recovery (Host Context)

If `devcontainer exec` fails:

1. Run `devcontainer up --workspace-folder .`
2. Retry with `devcontainer exec`
3. Never fall back to host execution

## Worktree Isolation

**Inside the devcontainer** (VS Code, Codespaces, or any container-native context): worktrees are local directories. Use them directly — no `docker` or `devcontainer` commands needed. Run lint, test, and build from the worktree path as normal.

**On the host**: each worktree should ideally have its own devcontainer instance (`devcontainer up --workspace-folder <worktree-path>`). This provides full isolation.

If multiple worktrees share a single devcontainer on the host (e.g., to save resources):

1. The container only mounts one workspace. Use `docker cp` to transfer files in for lint/test.
2. **Always restore the container's workspace after running commands**: `git checkout -- <files>` inside the container, or `docker cp` the originals back.
3. Never leave modified files from a worktree inside a shared container — subsequent agents will see stale or conflicting state.
4. Prefer dedicated containers when parallelizing agents to avoid this complexity entirely.
