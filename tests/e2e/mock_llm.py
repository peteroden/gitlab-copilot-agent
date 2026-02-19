"""Mock OpenAI-compatible LLM endpoint for E2E tests.

Returns a canned code review response in the format the agent expects.

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

CANNED_RESPONSE = f"""```json
{CANNED_REVIEW}
```

## Summary
This is a canned E2E test review. The code looks fine overall."""


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict:
    """OpenAI-compatible chat completions endpoint."""
    return {
        "id": "e2e-mock",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": CANNED_RESPONSE},
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
