"""Mock Jira REST API for E2E tests. Port 9997.

Returns one "AI Ready" issue on first search, empty on subsequent.
Records transitions and comments for test assertions.

Usage: uv run python tests/e2e/mock_jira.py
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

_issue_returned = False
transitions: list[dict] = []
comments: list[dict] = []

TRANSITION_MAP = {"21": "In Progress", "31": "In Review", "41": "Done"}


@app.get("/rest/api/3/search/jql")
async def search(jql: str = "") -> dict:
    global _issue_returned  # noqa: PLW0603
    if _issue_returned:
        return {"issues": [], "total": 0}
    _issue_returned = True
    return {
        "issues": [
            {
                "id": "10001",
                "key": "DEMO-1",
                "fields": {
                    "summary": "Add error handling",
                    "description": "Add try/except blocks",
                    "status": {"name": "AI Ready", "id": "10100"},
                    "assignee": None,
                    "labels": [],
                },
            }
        ],
        "total": 1,
    }


@app.get("/rest/api/3/issue/{key}/transitions")
async def get_transitions(key: str) -> dict:
    return {"transitions": [{"id": k, "name": v} for k, v in TRANSITION_MAP.items()]}


@app.post("/rest/api/3/issue/{key}/transitions")
async def post_transition(key: str, request: Request) -> JSONResponse:
    body = await request.json()
    tid = body.get("transition", {}).get("id", "")
    transitions.append({"key": key, "name": TRANSITION_MAP.get(tid, tid)})
    return JSONResponse(status_code=204, content=None)


@app.post("/rest/api/3/issue/{key}/comment")
async def post_comment(key: str, request: Request) -> dict:
    body = await request.json()
    comments.append({"key": key, "body": body})
    return {"id": "1"}


@app.get("/transitions")
async def get_recorded() -> list[dict]:
    return transitions


@app.delete("/transitions")
async def clear_transitions() -> dict:
    transitions.clear()
    return {"cleared": True}


@app.get("/comments")
async def get_recorded_comments() -> list[dict]:
    return comments


@app.delete("/comments")
async def clear_comments() -> dict:
    comments.clear()
    return {"cleared": True}


@app.post("/reset")
async def reset_state() -> dict:
    """Reset all state for test isolation."""
    global _issue_returned  # noqa: PLW0603
    _issue_returned = False
    transitions.clear()
    comments.clear()
    return {"reset": True}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9997)
