# Security Patterns Skill

## Purpose

Project-specific security patterns for the Blog Post API. Reference these when writing or reviewing authentication, database queries, and input handling.

## Use When

- Writing or reviewing database query implementations
- Writing or reviewing authentication/authorization code
- Validating input handling and error responses

## When Writing Code

Follow these patterns in all new and modified code:

- **Database queries**: Always use parameterized statements with `?` placeholders. Never use f-strings, `.format()`, or `+` concatenation in SQL.
- **Secrets**: Load from `os.environ` or FastAPI `Settings`. Never assign literal credential values in source code.
- **Authentication**: Use FastAPI `Security()` with `APIKeyHeader` or `HTTPBearer` for auth dependencies, and `Depends()` for general DI. Never accept API keys as query parameters.
- **Error responses**: Return structured `HTTPException` with safe `detail` messages. Never expose internal errors, stack traces, or SQL details to clients.
- **Logging**: Use the `logging` module. Never log credentials, API keys, or full exception tracebacks at INFO level or below.

## Database Queries

**Approved — parameterized statements:**
```python
cursor.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
cursor.execute("SELECT * FROM posts WHERE author = ?", (author,))
cursor.execute(
    "INSERT INTO posts (id, title, content, author) VALUES (?, ?, ?, ?)",
    (post_id, title, content, author),
)
```

**Forbidden — string concatenation (SQL injection risk):**
```python
cursor.execute(f"SELECT * FROM posts WHERE id = '{post_id}'")
cursor.execute("SELECT * FROM posts WHERE author = '" + author + "'")
```

## API Authentication

**Approved — environment-based secrets:**
```python
import os
from fastapi.security import HTTPBearer

API_KEY = os.environ["API_KEY"]
security = HTTPBearer()
```

**Forbidden — hardcoded secrets:**
```python
API_KEY = "sk_live_abc123"
```

## Error Responses

**Approved — structured error with no internal leakage:**
```python
raise HTTPException(status_code=400, detail="Invalid post ID format")
```

**Forbidden — leaking internals:**
```python
return {"error": str(exception)}  # Leaks stack trace or SQL details
```
