# TEST FIXTURE — intentionally contains issues (missing types, no auth) for the agent to review.
"""Blog Post API — main application."""

from fastapi import FastAPI, HTTPException

from demo_app import database
from demo_app.search import search_posts

app = FastAPI(title="Blog Post API")


@app.get("/search")
def search(q: str):
    return search_posts(q)


@app.get("/posts/{post_id}")
def get_post(post_id: str):
    post = database.get_post(post_id)
    if not post:
        raise HTTPException(status_code=404)
    return post
