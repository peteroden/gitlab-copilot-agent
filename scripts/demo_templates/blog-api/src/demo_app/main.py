"""Blog Post API — a demo FastAPI application."""

import logging

from fastapi import Depends, FastAPI, HTTPException

from demo_app.auth import verify_api_key
from demo_app.database import (
    create_post,
    delete_post,
    get_all_posts,
    get_post,
    get_posts_by_author,
)
from demo_app.models import PostCreate, PostResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="Blog Post API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/posts/{post_id}", response_model=PostResponse)
def read_post(post_id: str, _key: str = Depends(verify_api_key)) -> PostResponse:
    post = get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return PostResponse(**post)


@app.get("/posts", response_model=list[PostResponse])
def list_posts(author: str = "") -> list[PostResponse]:
    if author:
        return [PostResponse(**p) for p in get_posts_by_author(author)]
    return [PostResponse(**p) for p in get_all_posts()]


@app.post("/posts", status_code=201, response_model=PostResponse)
def new_post(post: PostCreate, _key: str = Depends(verify_api_key)) -> PostResponse:
    logger.info("Creating post: %s", post.title)
    result = create_post(post.title, post.content, post.author)
    return PostResponse(**result)


@app.delete("/posts/{post_id}")
def remove_post(post_id: str, _key: str = Depends(verify_api_key)) -> dict[str, bool]:
    delete_post(post_id)
    return {"deleted": True}
