# Project Review Standards

This project follows strict security and code quality standards.

## Security

- All database queries MUST use parameterized statements (never string concatenation or f-strings)
- No hardcoded credentials or secrets in source code — use environment variables
- Security issues are P0 — flag them with `[SECURITY]` severity
- Error responses must not leak internal details (stack traces, SQL errors, file paths)

## Code Quality

- All public functions must have docstrings following Google style
- All API endpoints must include error handling — no bare `except` or silent failures
- Use structured logging (`logging` module) instead of `print()` for all output
- Functions must have type hints on all parameters and return values

## Review Priorities

1. Security vulnerabilities (OWASP Top 10)
2. Bug risks and logic errors
3. Missing error handling
4. Type safety and documentation gaps
5. Performance concerns
