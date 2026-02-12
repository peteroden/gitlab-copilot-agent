# GitHub Copilot SDK — Python API Reference

Reference for the `github-copilot-sdk` Python package used in this project. SDK is in technical preview (as of Feb 2026).

## Installation

```bash
pip install github-copilot-sdk
# or
uv add github-copilot-sdk
```

**Requirements**: Python 3.9+, GitHub Copilot CLI installed and in PATH.

## CopilotClient

```python
from copilot import CopilotClient

client = CopilotClient({
    "cli_path": "copilot",       # Path to CLI binary (default: "copilot" or $COPILOT_CLI_PATH)
    "cli_url": None,             # URL of existing server (e.g., "localhost:8080") — skips spawning CLI
    "cwd": "/path/to/workdir",   # Working directory for CLI process
    "port": 0,                   # Server port for TCP mode (0 = random)
    "use_stdio": True,           # Use stdio transport (default: True)
    "log_level": "info",         # Log level
    "auto_start": True,          # Auto-start server on first use
    "auto_restart": True,        # Auto-restart on crash
    "github_token": "ghp_...",   # GitHub token — takes priority over other auth
    "use_logged_in_user": True,  # Use logged-in user auth (default: True, False when github_token set)
})

await client.start()
# ... use sessions ...
await client.stop()
```

## Sessions

```python
session = await client.create_session({
    "model": "gpt-5",                    # Required when using custom provider
    "session_id": "custom-id",           # Optional custom session ID
    "streaming": True,                   # Enable streaming delta events
    "tools": [my_tool],                  # Custom tools (see below)
    "system_message": {"content": "..."},# System message
    "provider": { ... },                 # BYOK provider config (see below)
    "hooks": { ... },                    # Session lifecycle hooks (see below)
    "infinite_sessions": {               # Context window management
        "enabled": True,
        "background_compaction_threshold": 0.80,
        "buffer_exhaustion_threshold": 0.95,
    },
    "on_user_input_request": handler,    # Handler for ask_user tool
})

# Send a prompt
await session.send({"prompt": "Review this code"})

# Access workspace path
session.workspace_path  # ~/.copilot/session-state/{session_id}/

# Clean up
await session.destroy()
```

## Custom Tools — @define_tool (Recommended)

```python
from pydantic import BaseModel, Field
from copilot import define_tool

class ReadFileParams(BaseModel):
    path: str = Field(description="Path to file to read")

@define_tool(description="Read a file from the repository")
async def read_file(params: ReadFileParams) -> str:
    content = Path(params.path).read_text()
    return content
```

Pass tools to session:
```python
session = await client.create_session({
    "model": "gpt-5",
    "tools": [read_file, list_directory, get_mr_diff],
})
```

## Custom Tools — Low-Level API

```python
from copilot import Tool

async def read_file_handler(invocation):
    path = invocation["arguments"]["path"]
    content = Path(path).read_text()
    return {
        "textResultForLlm": content,
        "resultType": "success",
        "sessionLog": f"Read {path}",
    }

read_file_tool = Tool(
    name="read_file",
    description="Read a file from the repository",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to file"},
        },
        "required": ["path"],
    },
    handler=read_file_handler,
)
```

## Event Handling

```python
import asyncio

done = asyncio.Event()

def on_event(event):
    match event.type.value:
        case "assistant.message_delta":
            print(event.data.delta_content, end="", flush=True)  # Streaming chunk
        case "assistant.message":
            print(event.data.content)   # Final complete message
        case "session.idle":
            done.set()                  # Session finished processing

session.on(on_event)
await session.send({"prompt": "..."})
await done.wait()
```

**Event types:**
- `assistant.message` — Final complete assistant response
- `assistant.message_delta` — Streaming chunk (`delta_content` field)
- `assistant.reasoning` — Final reasoning content (model-dependent)
- `assistant.reasoning_delta` — Streaming reasoning chunk
- `session.idle` — Session finished processing
- `session.compaction_start` / `session.compaction_complete` — Context compaction events

## Session Hooks

```python
session = await client.create_session({
    "model": "gpt-5",
    "hooks": {
        "on_pre_tool_use": on_pre_tool_use,
        "on_post_tool_use": on_post_tool_use,
        "on_user_prompt_submitted": on_user_prompt_submitted,
        "on_session_start": on_session_start,
        "on_session_end": on_session_end,
        "on_error_occurred": on_error_occurred,
    },
})
```

**Hook signatures:**

```python
async def on_pre_tool_use(input, invocation):
    # input["toolName"], input["toolArgs"]
    return {
        "permissionDecision": "allow",  # "allow", "deny", or "ask"
        "modifiedArgs": input.get("toolArgs"),
        "additionalContext": "Extra context",
    }

async def on_post_tool_use(input, invocation):
    # input["toolName"]
    return {"additionalContext": "Post-execution notes"}

async def on_error_occurred(input, invocation):
    # input["errorContext"], input["error"]
    return {"errorHandling": "retry"}  # "retry", "skip", or "abort"
```

## BYOK Provider Config

```python
# Azure OpenAI
session = await client.create_session({
    "model": "gpt-4",
    "provider": {
        "type": "azure",                                    # Must be "azure" for *.openai.azure.com
        "base_url": "https://my-resource.openai.azure.com", # Host only, no path
        "api_key": os.environ["AZURE_OPENAI_KEY"],
        "azure": {"api_version": "2024-10-21"},
    },
})

# OpenAI-compatible API
session = await client.create_session({
    "model": "gpt-5",
    "provider": {
        "type": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": os.environ["OPENAI_API_KEY"],
    },
})
```

**ProviderConfig fields:**
- `type` (str): `"openai"`, `"azure"`, or `"anthropic"`
- `base_url` (str): API endpoint URL (required)
- `api_key` (str): API key (optional for local providers)
- `bearer_token` (str): Bearer token (takes precedence over `api_key`)
- `wire_api` (str): `"completions"` or `"responses"` (default: `"completions"`)
- `azure.api_version` (str): Azure API version (default: `"2024-10-21"`)

## Key Constraints

- Copilot CLI binary must be in PATH (or specify `cli_path`)
- When using BYOK provider, `model` is **required**
- For Azure endpoints, use `type: "azure"`, not `type: "openai"`
- Azure `base_url` is just the host — SDK constructs the full path
- All operations are async — use `asyncio.run()` or an async framework
