"""Authentication service for user login and session management."""

import hashlib
import hmac
import time

from fake_repo.config import config
from fake_repo.db_models import User


class AuthenticationError(Exception):
    """Raised when authentication fails."""



def authenticate(token: str) -> User:
    """Verify a JWT token and return the associated user.

    Args:
        token: The JWT bearer token.

    Returns:
        The authenticated User object.

    Raises:
        AuthenticationError: If token is invalid or expired.
    """
    payload = _decode_jwt(token)
    if payload is None:
        raise AuthenticationError("Invalid token")
    if payload.get("exp", 0) < time.time():
        raise AuthenticationError("Token expired")
    user_id = payload.get("user_id")
    if user_id is None:
        raise AuthenticationError("Missing user_id in token")
    # In production, this would query the database
    return User(id=user_id, email=payload.get("email", ""), password_hash="")


def create_session(user: User) -> str:
    """Create a new JWT session token for a user."""
    payload = {
        "user_id": user.id,
        "email": user.email,
        "exp": time.time() + config.JWT_EXPIRY,
    }
    return _encode_jwt(payload)


def hash_password(raw: str) -> str:
    """Hash a password using SHA-256 with salt."""
    salt = config.SECRET_KEY
    return hashlib.sha256(f"{salt}{raw}".encode()).hexdigest()


def verify_password(raw: str, hashed: str) -> bool:
    """Verify a password against its hash."""
    return hmac.compare_digest(hash_password(raw), hashed)


def _decode_jwt(token: str) -> dict | None:
    """Decode a JWT token (simplified)."""
    # Simplified: in production, use a proper JWT library
    parts = token.split(".")
    if len(parts) != 3:
        return None
    return {"user_id": 1, "email": "user@example.com", "exp": time.time() + 3600}


def _encode_jwt(payload: dict) -> str:
    """Encode a JWT token (simplified)."""
    return f"header.{payload}.signature"
