"""Mock OpenAI-compatible LLM endpoint for E2E tests.

Returns canned responses based on task type (review vs coding).
The task type is inferred from the system prompt content.

Usage: uv run python tests/e2e/mock_llm.py [--port 9998]
"""

import json

from fastapi import FastAPI, Request

app = FastAPI()

# Canned review matching comment_parser.py's expected format
CANNED_REVIEW = json.dumps(
    [
        {
            "file": "app.py",
            "line": 1,
            "severity": "info",
            "comment": "E2E test review comment — import os is unused.",
            "suggestion": None,
            "suggestion_start_offset": 0,
            "suggestion_end_offset": 0,
        }
    ]
)

CANNED_REVIEW_RESPONSE = f"""```json
{CANNED_REVIEW}
```

## Summary
This is a canned E2E test review. The code looks fine overall."""

# Canned coding response matching CodingAgentOutput schema
CANNED_CODING_RESPONSE = """I've added a hello endpoint to the app.

```json
{
  "summary": "Added hello endpoint to app.py",
  "files_changed": ["app.py"]
}
```"""


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict:
    """OpenAI-compatible chat completions endpoint."""
    body = await request.json()
    messages = body.get("messages", [])
    system_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")

    # Pick response based on task type inferred from system prompt
    content = CANNED_CODING_RESPONSE if "files_changed" in system_text else CANNED_REVIEW_RESPONSE
    return {
        "id": "e2e-mock",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9998)
