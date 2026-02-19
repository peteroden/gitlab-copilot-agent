# Testing Guide

Test structure, shared fixtures, mocking patterns, coverage requirements, how to add tests.

---

## Test Structure

**Location**: `tests/` directory mirrors `src/gitlab_copilot_agent/`

**Naming**: Test files prefixed with `test_` (e.g., `test_webhook.py` for `webhook.py`)

**Framework**: pytest with pytest-asyncio for async tests

**Coverage**: ≥90% line coverage required (enforced by `pytest --cov-fail-under=90`)

---

## Shared Test Constants (`tests/conftest.py`)

**Purpose**: Centralized constants used across multiple tests. Prevents magic strings.

```python
GITLAB_URL = "https://gitlab.example.com"
GITLAB_TOKEN = "test-token"
WEBHOOK_SECRET = "test-secret"
GITHUB_TOKEN = "gho_test_token"
HEADERS = {"X-Gitlab-Token": WEBHOOK_SECRET}

# Jira constants
JIRA_URL = "https://jira.example.com"
JIRA_EMAIL = "bot@example.com"
JIRA_TOKEN = "test-jira-token"
JIRA_PROJECT_MAP_JSON = '{"mappings": {"PROJ": {...}}}'

PROJECT_ID = 42
MR_IID = 7
EXAMPLE_CLONE_URL = "https://gitlab.example.com/group/project.git"

DIFF_REFS = MRDiffRef(base_sha="aaa", start_sha="bbb", head_sha="ccc")
SAMPLE_DIFF = """@@ -1,3 +1,4 @@..."""  # Unified diff for position validation

MR_PAYLOAD: dict[str, Any] = { ... }  # Complete MR webhook payload
FAKE_REVIEW_OUTPUT = "```json\n[...]\n```\nOverall the changes look reasonable."
```

**Usage**: Import constants in test files:
```python
from tests.conftest import GITLAB_URL, MR_PAYLOAD, HEADERS
```

---

## Factory Functions (`tests/conftest.py`)

### `make_settings(**overrides)`

**Purpose**: Create Settings with test defaults. Override specific fields.

**Example**:
```python
from tests.conftest import make_settings

def test_config():
    settings = make_settings(gitlab_poll=True, gitlab_poll_interval=60)
    assert settings.gitlab_poll == True
    assert settings.gitlab_poll_interval == 60
```

**Default Values**: GITLAB_URL, GITLAB_TOKEN, WEBHOOK_SECRET, GITHUB_TOKEN

---

### `make_mr_payload(**attr_overrides)`

**Purpose**: Create MR webhook payload. Override `object_attributes` fields.

**Example**:
```python
from tests.conftest import make_mr_payload

def test_webhook():
    payload = make_mr_payload(action="update", oldrev="old-sha")
    assert payload["object_attributes"]["action"] == "update"
```

---

### `make_mr_changes(file_path, diff)`

**Purpose**: Create list of MRChange for testing.

**Example**:
```python
from tests.conftest import make_mr_changes, SAMPLE_DIFF

def test_comment_poster():
    changes = make_mr_changes("src/main.py", SAMPLE_DIFF)
    assert len(changes) == 1
    assert changes[0].new_path == "src/main.py"
```

---

## Shared Fixtures (`tests/conftest.py`)

### `env_vars(monkeypatch)`

**Purpose**: Set required env vars for Settings.

**Usage**: Automatically used by `client` fixture, can be used standalone.

**Example**:
```python
def test_settings(env_vars):
    settings = Settings()  # Loads from env
    assert settings.gitlab_url == GITLAB_URL
```

---

### `client(env_vars) -> AsyncClient`

**Purpose**: AsyncClient wired to FastAPI app with test settings.

**Sets**:
- `app.state.settings` → `make_settings()`
- `app.state.executor` → `LocalTaskExecutor()`
- `app.state.repo_locks` → `RepoLockManager()`
- `app.state.dedup_store` → `MemoryDedup()`
- `app.state.review_tracker` → `ReviewedMRTracker()`
- `app.state.allowed_project_ids` → `None`

**Example**:
```python
@pytest.mark.asyncio
async def test_webhook_endpoint(client):
    response = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
    assert response.status_code == 200
```

---

## Mocking Strategy

### AsyncMock for Async Functions

**Library**: `unittest.mock.AsyncMock`

**Pattern**:
```python
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_review_flow(client, monkeypatch):
    mock_execute = AsyncMock(return_value=FAKE_REVIEW_OUTPUT)
    monkeypatch.setattr("gitlab_copilot_agent.orchestrator.TaskExecutor.execute", mock_execute)
    
    response = await client.post("/webhook", json=MR_PAYLOAD, headers=HEADERS)
    assert mock_execute.called
```

---

### Monkeypatch for Environment Variables

**Pattern**:
```python
def test_jira_config(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    settings = Settings()
    assert settings.jira is not None
```

---

### Monkeypatch for Module Functions

**Pattern**:
```python
@pytest.mark.asyncio
async def test_git_clone(monkeypatch):
    mock_clone = AsyncMock(return_value=Path("/tmp/test-repo"))
    monkeypatch.setattr("gitlab_copilot_agent.git_operations.git_clone", mock_clone)
    
    result = await some_function_that_clones()
    mock_clone.assert_called_once_with(
        "https://gitlab.com/group/project.git",
        "main",
        "token"
    )
```

---

## Test Categories

### Unit Tests

**Purpose**: Test individual functions/classes in isolation.

**Pattern**: Mock all external dependencies (API clients, filesystem, network).

**Example**: `test_comment_parser.py` — tests `parse_review()` with various inputs.

**Coverage**: High (≥95%) — unit tests are fast and thorough.

---

### Integration Tests

**Purpose**: Test multiple components together with real interactions.

**Pattern**: Mock only external services (GitLab API, Jira API, Copilot SDK).

**Example**: `test_webhook.py` — tests webhook endpoint → orchestrator → (mocked) executor.

**Coverage**: Medium (80-90%) — validates integration points.

---

### E2E Tests

**Purpose**: Test the full deployed agent in k3d against mock services on the host.

**Architecture**:
```
Host (mock services)                 k3d cluster
mock_gitlab.py:9999  ◄── HTTP ──── agent pod (GitLab API + git clone/push)
mock_llm.py:9998     ◄── HTTP ──── copilot SDK (OpenAI-compatible)
mock_jira.py:9997    ◄── HTTP ──── jira poller (search, transitions, comments)
```

**Mock services** (`tests/e2e/`):
- `mock_gitlab.py` — GitLab REST API + git HTTP server (clone + smart push) + MR creation
- `mock_llm.py` — OpenAI-compatible `/v1/chat/completions` returning canned responses
- `mock_jira.py` — Jira REST API: issue search, transitions, comments with test assertion endpoints

**Test flows** (`tests/e2e/run.sh`):
1. **Webhook MR Review** — sends MR webhook → polls for review comments on mock GitLab
2. **Jira Polling** — agent polls mock Jira for "AI Ready" issues → transitions to "In Progress" → coding task → push → MR creation → "In Review" transition → Jira comment
3. **/copilot Command** — sends note webhook with `/copilot <instruction>` → polls for agent response comment on mock GitLab

**Run locally**:
```bash
make e2e-up      # Create k3d cluster
make e2e-test    # Build, deploy, start mocks, run test
make e2e-down    # Teardown
```

**CI**: `.github/workflows/e2e.yml` — blocking on PRs. Auto-detects Docker gateway IP for `hostAliases`.

**Key config**: `ALLOW_HTTP_CLONE=true` enables HTTP git clone (mock git server). `hostAliases` injects `host.k3d.internal` into pod `/etc/hosts`.

**Not Implemented**: No live external service tests (GitLab/Jira/Copilot APIs).

---

### K8s Integration Tests

**Marker**: `@pytest.mark.k8s`

**Purpose**: Test KubernetesTaskExecutor with real K8s cluster.

**Skipped By Default**: `pytest -m "not k8s"` (enforced in pytest.ini)

**Run Manually**: `pytest -m k8s` (requires running k3d cluster)

**Example**: `test_k8s_integration.py`

---

## Coverage Requirements

**Tool**: pytest-cov

**Threshold**: 90% line coverage (enforced by `--cov-fail-under=90`)

**Config**: `pyproject.toml` → `[tool.pytest.ini_options]`

```toml
addopts = "--cov=gitlab_copilot_agent --cov-report=term-missing --cov-fail-under=90"
```

**Check Coverage**:
```bash
uv run pytest tests/ --cov --cov-report=term-missing
```

**Missing Lines**: Report shows uncovered lines (focus testing efforts there).

---

## How to Add a Test

### Step 1: Identify Module

**Example**: Adding feature to `comment_parser.py`

---

### Step 2: Create or Extend Test File

**Location**: `tests/test_comment_parser.py`

**Structure**:
```python
"""Tests for comment_parser.py"""
import pytest
from gitlab_copilot_agent.comment_parser import parse_review

def test_parse_review_with_json():
    raw = '```json\n[{"file": "a.py", "line": 1, "severity": "error", "comment": "Bug"}]\n```\nSummary'
    result = parse_review(raw)
    assert len(result.comments) == 1
    assert result.comments[0].file == "a.py"
    assert result.summary == "Summary"
```

---

### Step 3: Use Shared Fixtures/Constants

**Import from conftest.py**:
```python
from tests.conftest import make_settings, GITLAB_URL

@pytest.mark.asyncio
async def test_gitlab_client(monkeypatch):
    settings = make_settings()
    # Use GITLAB_URL in test
```

---

### Step 4: Mock External Dependencies

**Example**: Testing orchestrator without calling real Copilot SDK:
```python
from unittest.mock import AsyncMock
from tests.conftest import FAKE_REVIEW_OUTPUT

@pytest.mark.asyncio
async def test_orchestrator(monkeypatch):
    mock_execute = AsyncMock(return_value=FAKE_REVIEW_OUTPUT)
    monkeypatch.setattr("gitlab_copilot_agent.task_executor.LocalTaskExecutor.execute", mock_execute)
    
    # Test orchestrator flow
```

---

### Step 5: Test Error Paths

**Pattern**: Test both success and failure cases.

**Example**:
```python
def test_parse_review_invalid_json():
    raw = "```json\n{invalid json}\n```"
    result = parse_review(raw)
    assert len(result.comments) == 0  # Fallback to summary
    assert "invalid json" in result.summary
```

---

### Step 6: Run Tests

**All Tests**:
```bash
uv run pytest tests/
```

**Single File**:
```bash
uv run pytest tests/test_comment_parser.py
```

**Single Test**:
```bash
uv run pytest tests/test_comment_parser.py::test_parse_review_with_json
```

**With Coverage**:
```bash
uv run pytest tests/ --cov --cov-report=term-missing
```

---

## Test Patterns

### Async Test

**Decorator**: `@pytest.mark.asyncio`

**Pattern**:
```python
@pytest.mark.asyncio
async def test_async_function():
    result = await some_async_function()
    assert result == expected
```

---

### Parametrized Test

**Decorator**: `@pytest.mark.parametrize`

**Pattern**:
```python
@pytest.mark.parametrize("input,expected", [
    ("foo", "FOO"),
    ("bar", "BAR"),
])
def test_uppercase(input, expected):
    assert input.upper() == expected
```

---

### Exception Testing

**Pattern**:
```python
def test_invalid_url():
    with pytest.raises(ValueError, match="Invalid URL"):
        _validate_clone_url("file:///etc/passwd")
```

---

### Temporary Directory

**Fixture**: `tmp_path` (pytest built-in)

**Pattern**:
```python
def test_file_operations(tmp_path):
    file = tmp_path / "test.txt"
    file.write_text("content")
    assert file.read_text() == "content"
```

---

## Debugging Tests

### Run with Verbose Output

```bash
uv run pytest tests/ -v
```

---

### Print Statements

```python
def test_something():
    print(f"Debug: {value}")  # Shows in pytest output with -s
    assert value == expected
```

**Run with Output**:
```bash
uv run pytest tests/test_file.py -s
```

---

### Debugger

**Add Breakpoint**:
```python
def test_something():
    breakpoint()  # Drops into pdb
    assert value == expected
```

**Run**:
```bash
uv run pytest tests/test_file.py -s
```

---

### Failed Test Details

```bash
uv run pytest tests/ -vv --tb=short
```

**Options**:
- `-vv`: Very verbose
- `--tb=short`: Short traceback
- `--tb=long`: Full traceback

---

## Continuous Integration

**Runs On**: Every push, every PR (via GitHub Actions)

**Commands**:
```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run pytest tests/ --cov --cov-fail-under=90
```

**Failure Conditions**:
- Linter errors
- Formatter violations
- Type errors
- Coverage below 90%
- Any test failure

---

## Common Test Failures

### Coverage Below 90%

**Cause**: New code added without tests.

**Fix**: Add tests for uncovered lines (see `--cov-report=term-missing` output).

---

### AsyncMock Not Awaited

**Error**: `RuntimeWarning: coroutine was never awaited`

**Cause**: Forgot to `await` async mock.

**Fix**:
```python
# Wrong
mock = AsyncMock(return_value="result")
result = mock()

# Right
mock = AsyncMock(return_value="result")
result = await mock()
```

---

### Pydantic Validation Error

**Error**: `ValidationError: 1 validation error for Settings`

**Cause**: Missing required env var in test.

**Fix**: Use `env_vars` fixture or `make_settings()`.

---

### Fixture Not Found

**Error**: `fixture 'client' not found`

**Cause**: Missing import or fixture not in conftest.py.

**Fix**: Check conftest.py has fixture defined, use `@pytest.fixture` decorator.

---

## Best Practices

1. **Use shared constants** from `conftest.py` — no magic strings
2. **Mock at boundaries** — mock external services, not internal modules
3. **Test one thing** — unit tests should be focused and fast
4. **Name descriptively** — `test_webhook_validates_hmac` better than `test_webhook`
5. **Test error paths** — success + failure cases
6. **Avoid sleep()** — use `AsyncMock` with deterministic control flow
7. **Clean up** — use `tmp_path`, context managers, fixtures for cleanup
8. **DRY test setup** — use factory functions, not copy-paste
9. **Assert specifics** — `assert x == "expected"` better than `assert x`
10. **Document why** — comments explain unusual test setup or edge cases

---

## Example: Adding a Test for New Feature

**Feature**: Add `parse_jira_description()` to `jira_client.py`

**Test File**: `tests/test_jira_client.py`

```python
"""Tests for jira_client.py"""
import pytest
from gitlab_copilot_agent.jira_client import parse_jira_description

def test_parse_jira_description_plain_text():
    """Plain text description returned as-is."""
    result = parse_jira_description("This is a plain text description")
    assert result == "This is a plain text description"

def test_parse_jira_description_adf():
    """ADF dict converted to plain text."""
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}
        ]
    }
    result = parse_jira_description(adf)
    assert result == "Hello"

def test_parse_jira_description_none():
    """None returned as empty string."""
    result = parse_jira_description(None)
    assert result == ""
```

**Run**:
```bash
uv run pytest tests/test_jira_client.py::test_parse_jira_description_plain_text -v
```

**Check Coverage**:
```bash
uv run pytest tests/test_jira_client.py --cov=gitlab_copilot_agent.jira_client --cov-report=term-missing
```
