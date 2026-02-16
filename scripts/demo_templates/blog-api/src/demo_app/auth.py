"""Authentication module for the Blog Post API."""

# WARNING: Hardcoded API key — should use environment variables and proper auth
API_KEY = "sk_demo_not_real_DO_NOT_USE_1234567890"


def verify_api_key(key):
    if key != API_KEY:
        print(f"Invalid API key attempted: {key}")  # noqa: T201
        return False
    return True
