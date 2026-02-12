---
name: test-quality
description: Enforce test quality standards — coverage, shared fixtures, no magic strings. Use this when writing, reviewing, or refactoring tests.
---

## Coverage

- Enforce `--cov-fail-under=90` in `pyproject.toml`:
  ```toml
  [tool.pytest.ini_options]
  addopts = "--cov=my_package --cov-report=term-missing --cov-fail-under=90"
  ```
- Run `pytest --cov-report=term-missing` to identify uncovered lines before submitting.

## No Magic Strings

❌ Bad:
```python
def test_webhook(client):
    resp = client.post("/webhook", headers={"X-Gitlab-Token": "test-secret"}, json={
        "object_kind": "merge_request",
        "user": {"id": 1, "username": "jdoe"},
        ...
    })
```

✅ Good:
```python
from tests.conftest import HEADERS, MR_PAYLOAD

def test_webhook(client):
    resp = client.post("/webhook", headers=HEADERS, json=MR_PAYLOAD)
```

Rules:
- If a string appears in more than one test, it must be a named constant.
- Constants live in `conftest.py` or a dedicated `tests/constants.py`.
- Name constants descriptively: `GITLAB_URL`, `WEBHOOK_SECRET`, `MR_PAYLOAD`.

## conftest.py Structure

```python
# tests/conftest.py — single source of truth for test data

# Constants
GITLAB_URL = "https://gitlab.example.com"
GITLAB_TOKEN = "test-token"
WEBHOOK_SECRET = "test-secret"
HEADERS = {"X-Gitlab-Token": WEBHOOK_SECRET}

# Factory functions — tests override only what they care about
def make_settings(**overrides):
    defaults = {"gitlab_url": GITLAB_URL, "gitlab_token": GITLAB_TOKEN, ...}
    return Settings(**(defaults | overrides))

def make_mr_payload(**attr_overrides):
    payload = {**MR_PAYLOAD}
    if attr_overrides:
        payload["object_attributes"] = {**payload["object_attributes"], **attr_overrides}
    return payload

# Shared fixtures
@pytest.fixture
def env_vars(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", GITLAB_URL)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", WEBHOOK_SECRET)
```

## Anti-Patterns

| Anti-pattern | Fix |
|---|---|
| Same env var setup in 3+ test files | Move to `conftest.py` `env_vars` fixture |
| Payload dict copy-pasted between files | Single `MR_PAYLOAD` constant in conftest |
| `client` fixture duplicated | Single shared fixture in conftest |
| Inline `"https://gitlab.example.com"` | `GITLAB_URL` constant |
| Testing that `pydantic.ValidationError` is raised by Pydantic | Test our validation logic, not Pydantic itself |
| No `--cov-fail-under` in config | Add to `pyproject.toml` immediately |

## Test Layers

| Layer | What to test | Mocking | Location |
|---|---|---|---|
| Unit | Single function/class logic | Mock all deps | `tests/test_<module>.py` |
| Integration | Module wiring (webhook → orchestrator → poster) | Mock external services | `tests/test_integration.py` |
| E2E | Full service with real services | None or containers | `tests/test_e2e.py` or `scripts/` |

## Checklist

Before submitting tests, verify:

- [ ] No string literal appears in more than one test file
- [ ] All shared setup is in `conftest.py`
- [ ] Factory functions used for complex test data
- [ ] `pytest --cov-report=term-missing` shows ≥90% coverage
- [ ] Tests assert behavior, not implementation details
- [ ] Mocks are at the boundary, not on internals
