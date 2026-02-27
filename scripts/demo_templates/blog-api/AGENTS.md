# Agent Instructions

## When Writing Code

1. **Always** use parameterized queries (`?` placeholders) for all database operations — never use string interpolation or concatenation in SQL
2. Load secrets and credentials from environment variables (`os.environ`) — never hardcode them
3. Use FastAPI `Depends()` for shared dependencies and `Security()` for authentication — never pass API keys as query parameters
4. Use Pydantic models for all request/response schemas
5. Use Python's `logging` module — never use `print()` for application output
6. Use context managers (`with` statements) for database connections and file handles
7. Add type hints to all function signatures including return types
8. Raise `HTTPException` with descriptive `detail` messages for error responses — never return raw dicts with error keys

## When Reviewing Code

1. Prioritize security vulnerabilities over style issues
2. Reference our security patterns (see `.github/skills/security-patterns/SKILL.md`) for approved and forbidden code patterns
3. Suggest fixes that align with FastAPI best practices
4. When suggesting logging changes, use Python's `logging` module with structured context
5. Flag any changes that might break API contracts with `[BREAKING]` tag
6. All database operations must use parameterized queries — flag any string interpolation in SQL as `[SECURITY]`
