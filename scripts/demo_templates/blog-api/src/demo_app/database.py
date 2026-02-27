"""Database operations for the Blog Post API."""

import sqlite3
import uuid

DB_PATH = "posts.db"


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT NOT NULL
        )"""
    )
    return conn


def get_post(post_id: str) -> dict[str, str] | None:
    with _get_connection() as conn:
        result = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if result:
        return dict(result)
    return None


def get_all_posts() -> list[dict[str, str]]:
    with _get_connection() as conn:
        results = conn.execute("SELECT * FROM posts").fetchall()
    return [dict(row) for row in results]


def get_posts_by_author(author: str) -> list[dict[str, str]]:
    with _get_connection() as conn:
        results = conn.execute("SELECT * FROM posts WHERE author = ?", (author,)).fetchall()
    return [dict(row) for row in results]


def create_post(title: str, content: str, author: str) -> dict[str, str]:
    post_id = str(uuid.uuid4())
    with _get_connection() as conn:
        conn.execute(
            "INSERT INTO posts (id, title, content, author) VALUES (?, ?, ?, ?)",
            (post_id, title, content, author),
        )
    return {"id": post_id, "title": title, "content": content, "author": author}


def delete_post(post_id: str) -> None:
    with _get_connection() as conn:
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
