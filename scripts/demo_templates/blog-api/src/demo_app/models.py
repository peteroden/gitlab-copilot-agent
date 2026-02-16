"""Data models for the Blog Post API."""

from pydantic import BaseModel


class PostCreate(BaseModel):
    title: str
    content: str
    author: str


class PostResponse(BaseModel):
    id: str
    title: str
    content: str
    author: str


class PostList(BaseModel):
    posts: list
    total: int
