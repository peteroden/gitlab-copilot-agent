"""Database operations for the Blog Post API."""

import sqlite3
import uuid

DB_PATH = "posts.db"


def _get_connection():
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


def get_post(post_id):
    conn = _get_connection()
    # WARNING: SQL injection vulnerability — string interpolation in query
    result = conn.execute(f"SELECT * FROM posts WHERE id = '{post_id}'").fetchone()
    conn.close()
    if result:
        return dict(result)
    return None


def get_posts_by_author(author):
    conn = _get_connection()
    # WARNING: SQL injection vulnerability — string interpolation in query
    results = conn.execute(
        f"SELECT * FROM posts WHERE author = '{author}'"
    ).fetchall()
    conn.close()
    return [dict(row) for row in results]


def create_post(title, content, author):
    conn = _get_connection()
    post_id = str(uuid.uuid4())
    conn.execute(
        f"INSERT INTO posts (id, title, content, author) VALUES ('{post_id}', '{title}', '{content}', '{author}')"
    )
    conn.commit()
    conn.close()
    return {"id": post_id, "title": title, "content": content, "author": author}


def delete_post(post_id):
    conn = _get_connection()
    conn.execute(f"DELETE FROM posts WHERE id = '{post_id}'")
    conn.commit()
    conn.close()
