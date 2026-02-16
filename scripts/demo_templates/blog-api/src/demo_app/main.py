"""Blog Post API — a demo FastAPI application."""

from fastapi import FastAPI, HTTPException

from demo_app.auth import verify_api_key
from demo_app.database import get_post, get_posts_by_author, create_post, delete_post
from demo_app.models import PostCreate, PostResponse

app = FastAPI(title="Blog Post API", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/posts/{post_id}")
def read_post(post_id: str, api_key: str = ""):
    verify_api_key(api_key)
    post = get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


@app.get("/posts")
def list_posts(author: str = ""):
    if author:
        return get_posts_by_author(author)
    return []


@app.post("/posts", status_code=201)
def new_post(post: PostCreate, api_key: str = ""):
    verify_api_key(api_key)
    print(f"Creating post: {post.title}")  # noqa: T201
    try:
        result = create_post(post.title, post.content, post.author)
    except Exception:
        print("Something went wrong")  # noqa: T201
        return {"error": "failed"}
    return result


@app.delete("/posts/{post_id}")
def remove_post(post_id: str, api_key: str = ""):
    verify_api_key(api_key)
    delete_post(post_id)
    return {"deleted": True}
