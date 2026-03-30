"""Search functionality for the Blog Post API."""

from demo_app.database import _get_connection


def search_posts(query):
    conn = _get_connection()
    results = conn.execute(
        f"SELECT * FROM posts WHERE title LIKE '%{query}%'"
        f" OR content LIKE '%{query}%'"
    ).fetchall()
    conn.close()
    return [dict(row) for row in results]
