# Python Code Standards

- Use type hints for all function signatures (parameters and return types)
- Use `snake_case` for all identifiers (functions, variables, modules)
- Never use `print()` for logging — use the `logging` module with structured context
- Prefer `pathlib.Path` over `os.path` for file operations
- All exceptions must be logged before being raised or handled
- Use context managers (`with` statements) for resource management (files, connections, database cursors)
- Prefer list comprehensions over `map()`/`filter()` for readability
- Load secrets and credentials from environment variables — never hardcode tokens, API keys, or passwords
- Use parameterized queries (`?` placeholders) for all database operations — never use string interpolation in SQL
- Use FastAPI `Depends()` for dependency injection and `Security()` for authentication dependencies
