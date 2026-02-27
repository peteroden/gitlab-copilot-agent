"""Authentication module for the Blog Post API."""

import logging
import os

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "")

_api_key_header = APIKeyHeader(name="X-API-Key")


def verify_api_key(key: str = Security(_api_key_header)) -> str:
    """Validate the API key from the request header. Use as a FastAPI dependency."""
    if not API_KEY:
        logger.error("API_KEY environment variable is not set")
        raise HTTPException(status_code=500, detail="Server misconfigured")
    if key != API_KEY:
        logger.warning("Invalid API key attempted")
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key
