"""Authentication middleware for protecting API routes."""

from collections.abc import Callable
from functools import wraps
from typing import Any

from fake_repo.auth_service import AuthenticationError, authenticate


def require_auth(handler: Callable) -> Callable:
    """Decorator that enforces authentication on a route handler.

    Extracts the bearer token from the request headers,
    validates it, and injects the authenticated user.
    """

    @wraps(handler)
    async def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
        token = extract_token(request.headers)
        if token is None:
            return {"status": 401, "error": "Missing authorization header"}
        try:
            user = authenticate(token)
        except AuthenticationError as e:
            return {"status": 401, "error": str(e)}
        request.user = user
        return await handler(request, *args, **kwargs)

    return wrapper


def extract_token(headers: dict) -> str | None:
    """Extract the bearer token from request headers."""
    auth_header = headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None
