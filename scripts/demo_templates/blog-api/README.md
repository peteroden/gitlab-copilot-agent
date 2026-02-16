# Blog Post API

A simple blog post CRUD API built with FastAPI.

## Endpoints

- `GET /health` — Health check
- `GET /posts/{post_id}` — Get a post by ID
- `GET /posts?author=name` — List posts by author
- `POST /posts` — Create a new post
- `DELETE /posts/{post_id}` — Delete a post

## Running

```bash
pip install -e .
uvicorn demo_app.main:app --reload
```

## Testing

```bash
pip install -e ".[dev]"
pytest
```
