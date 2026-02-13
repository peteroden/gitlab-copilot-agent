---
name: devcontainer-setup
description: Templates and process for creating devcontainer configurations. Use this when setting up a new project, adding devcontainer support, or creating devcontainers for worktrees.
---

Every project must have a devcontainer. Agents execute all commands inside devcontainers — never on the host.

## Devcontainer Tiers

### Yolo (Developer Agent) — No Network

For sandboxed development. The agent cannot reach the internet or install packages at runtime.

```jsonc
// .devcontainer/devcontainer.json
{
  "name": "${project-name}-yolo",
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu",
  "runArgs": ["--network=none"],
  "features": {
    // Add only what the project needs
  },
  "postCreateCommand": "echo 'Yolo devcontainer ready — no network access'",
  "customizations": {
    "vscode": {
      "extensions": ["GitHub.copilot"]
    }
  }
}
```

### Interactive — With Network

For agents that need human-approved network access (Architect, Product, Designer, Orchestrator).

```jsonc
// .devcontainer/devcontainer.json
{
  "name": "${project-name}",
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu",
  "features": {
    // Add only what the project needs
  },
  "postCreateCommand": "echo 'Interactive devcontainer ready'",
  "customizations": {
    "vscode": {
      "extensions": ["GitHub.copilot"]
    }
  }
}
```

## Language Features

Add only what the project requires:

```jsonc
// TypeScript
"ghcr.io/devcontainers/features/node:1": {}

// Python (with uv)
"ghcr.io/devcontainers/features/python:1": {}

// Rust
"ghcr.io/devcontainers/features/rust:1": {}

// C/C++
"ghcr.io/devcontainers/features/common-utils:1": {}
// Install build tools via postCreateCommand:
// "postCreateCommand": "apt-get update && apt-get install -y build-essential clang clang-format clang-tidy cppcheck"

// Go
"ghcr.io/devcontainers/features/go:1": {}
```

## ARM64 (Apple Silicon) Notes

- `mcr.microsoft.com/devcontainers/universal:2` does NOT support ARM64.
- Use `mcr.microsoft.com/devcontainers/base:ubuntu` instead.
- All `ghcr.io/devcontainers/features/*` features support ARM64.

## Pre-flight Check

See `.github/instructions/devcontainer.instructions.md` for context detection and command rules before running any dev command.

## Setup Process

### New Project

1. Create `.devcontainer/devcontainer.json` with the appropriate tier template.
2. Add language features needed for the project.
3. Test: `devcontainer up --workspace-folder .`
4. Verify: `devcontainer exec --workspace-folder . <build-command>`

### Worktree Task

1. Create worktree: `git worktree add ../worktrees/<branch> -b <branch>`
2. Start container: `devcontainer up --workspace-folder ../worktrees/<branch>`
3. Run commands: `devcontainer exec --workspace-folder ../worktrees/<branch> <command>`

## Developer Experience Checklist

The devcontainer must support:
- [ ] One-command build
- [ ] One-command test (all levels)
- [ ] One-command run
- [ ] Debugger attachment
- [ ] Auto-install dependencies on container start
- [ ] Local config via `.env` (not production credentials)

## postCreateCommand Examples

```jsonc
// TypeScript
"postCreateCommand": "npm ci"

// Python with uv
"postCreateCommand": "uv sync"

// Rust
"postCreateCommand": "cargo fetch"

// C/C++
"postCreateCommand": "apt-get update && apt-get install -y build-essential cmake"
```
