# Agent Instructions

When reviewing this codebase:

1. Prioritize security vulnerabilities over style issues
2. Reference our security patterns (see `.github/skills/security-patterns/SKILL.md`) for approved and forbidden code patterns
3. Suggest fixes that align with FastAPI best practices
4. When suggesting logging changes, use Python's `logging` module with structured context
5. Flag any changes that might break API contracts with `[BREAKING]` tag
6. All database operations must use parameterized queries — flag any string interpolation in SQL as `[SECURITY]`
