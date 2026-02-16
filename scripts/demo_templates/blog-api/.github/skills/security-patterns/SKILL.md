# Security Patterns Skill

## Purpose

Project-specific security patterns for the Blog Post API. Reference these when reviewing authentication, database queries, and input handling.

## Use When

- Reviewing database query implementations
- Checking authentication/authorization code
- Validating input handling and error responses

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
