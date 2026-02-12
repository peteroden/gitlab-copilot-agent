# Agent Governance

This document defines how AI agents operate on projects that adopt this governance model. It balances agent autonomy with safety, security, and code quality.

## Core Principles

1. All coding happens inside devcontainers — never on the host.
2. Each agent works on exactly 1 task.
3. Each task gets its own git worktree.
4. PRs must be human-reviewable (≤200 diff lines).
5. Agents have distinct roles with clear ownership boundaries.
6. No slop — all output must be small, well-written, succinct, and easy to understand.

## Agent Tiers

### Yolo Agents (Developer)

- Full auto-approve within their sandbox.
- No network access — enforced via `"network": "none"` in devcontainer.
- File access limited to their worktree.
- Can only use pre-installed packages.
- Can commit to their branch, never merge to main.

### Interactive Agents (Product, Architect, Designer, Orchestrator)

- Require human approval for installing packages, accessing the internet, or any action outside their worktree.
- Read-only access to shared config/libraries (mounted as volumes).
- Can commit to their branch, never merge to main.

#### Approval Categories

| Level | Actions |
|-------|---------|
| 🟢 Auto-approve | Read files, run tests, build, lint |
| 🟡 Prompt once | Install pinned dependencies, access known APIs |
| 🔴 Always prompt | Arbitrary network access, new packages, secrets |

## Isolation Model

- **1 task = 1 agent = 1 worktree = 1 devcontainer instance.**
- Worst case failure: roll back the worktree branch.
- Each worktree gets its own running devcontainer.

## Task Scoping

- Tasks must produce a PR of **≤200 diff lines** (additions + deletions).
- Diff lines are measured, not total file lines.
- If a task would exceed 200 diff lines, the agent must **stop and ask to split** before writing code.

## Stacked PRs

When work exceeds 200 diff lines after scoping:

1. Split into a stack of PRs.
2. Each PR must be **fully functional**, **standalone**, and **under 200 diff lines**.
3. PR 2 branches off PR 1, PR 3 off PR 2, etc.
4. Each PR is reviewed and merged independently, bottom-up.
5. If a lower PR needs changes, rebase the stack.

## Agent Roles

See custom agent profiles in `.github/agents/` for detailed role definitions:

- **Product** (`product.agent.md`) — Owns the *what* and *why*. Writes BDDs and PDDs.
- **Architect** (`architect.agent.md`) — Owns the *how* at a system level.
- **Designer** (`designer.agent.md`) — Owns user experience and interface design.
- **Orchestrator** (`orchestrator.agent.md`) — Owns the *when* and *who*.
- **Developer** (`developer.agent.md`) — Owns implementation.

### Responsibility Matrix

| Concern | Product | Architect | Designer | Orchestrator | Developer |
|---------|---------|-----------|----------|--------------|-----------|
| Requirements & scope | **Owner** | Consulted | Consulted | Informed | Informed |
| System design & ADRs | Informed | **Owner** | Consulted | Informed | Informed |
| UI/UX design | Consulted | Consulted | **Owner** | Informed | Informed |
| Task breakdown | Consulted | Consulted | — | **Owner** | Informed |
| Worktree/devcontainer mgmt | — | — | — | **Owner** | — |
| Code implementation | — | — | — | — | **Owner** |
| Writing tests | — | — | — | — | **Owner** |
| SOLID compliance | — | Reviews | — | — | **Owner** |
| OWASP review | — | Reviews | — | — | **Owner** |
| Observability (strategy) | — | **Owner** | — | — | Implements |
| Documentation | Reviews | Reviews | Reviews | — | **Owner** |
| PR sequencing | — | — | — | **Owner** | — |
| Governance enforcement | — | — | — | **Owner** | Follows |

## Code Quality & Security

### SOLID Principles

All code follows: Single Responsibility, Open/Closed, Liskov Substitution, Interface Segregation, Dependency Inversion.

### Least Privilege

- **Code**: request only the permissions needed. No broad tokens, no wildcard permissions, no running as root unless required.
- **Process**: agents operate with minimum access for their task.

### Security

- No secrets in source code.
- Validate and sanitize all inputs.
- Use parameterized queries.
- Encrypt sensitive data at rest and in transit.
- Pin dependency versions.

### OWASP Review

Every PR is reviewed against the OWASP Top 10. Code touching auth, authorization, data storage, or external communication requires explicit human review.

## Testing

- Code is incomplete without tests.
- Unit tests: >90% coverage, no external dependencies, run on every PR.
- Integration tests: validate component interactions.
- E2E tests: test full user workflows.
- Test **our code**, not third-party libraries. Mock at the boundary.
- Tests written alongside code, not after.

## Conventions

### Naming & Style

| Language | Style Guide | Linter / Formatter | Static Analysis |
|----------|-------------|---------------------|-----------------|
| TypeScript | Google TS Style | ESLint + Prettier | `tsc --strict` |
| Python | PEP 8 | Ruff, uv | mypy `--strict` |
| C | Linux kernel style | clang-format + clang-tidy | cppcheck, `-Wall -Wextra -Werror` |
| C++ | C++ Core Guidelines | clang-format + clang-tidy | cppcheck, `-Wall -Wextra -Werror` |
| Rust | Rust API Guidelines | rustfmt | clippy (deny warnings) |

### Commits

[Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <summary>`

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`, `perf`, `build`

### Branches

Format: `<type>/<ticket-id>-<short-description>` (e.g., `feature/PROJ-123-add-oauth-login`)

### Definition of Done

- Code builds with zero errors and warnings.
- Linters and formatters pass.
- Unit, integration, and e2e tests written and passing.
- Documentation updated.
- PR opened with conventional commits.
- PR ≤200 diff lines.
- OWASP self-review completed.
- No new dependencies without justification.
