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
