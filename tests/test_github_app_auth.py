"""Tests for GitHub App JWT authentication and installation token caching."""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from mira.exceptions import WebhookError
from mira.github_app.auth import GitHubAppAuth


@pytest.fixture
def rsa_private_key() -> str:
    """Generate a test RSA private key in PEM format."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()


@pytest.fixture
def app_auth(rsa_private_key: str) -> GitHubAppAuth:
    return GitHubAppAuth(app_id="12345", private_key=rsa_private_key)


def test_generate_jwt_structure(app_auth: GitHubAppAuth) -> None:
    """JWT has correct iss, iat, exp fields and is RS256-signed."""
    token = app_auth._generate_jwt()

    # Decode without verification to inspect claims
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "12345"
    assert "iat" in claims
    assert "exp" in claims

    now = int(time.time())
    assert claims["iat"] <= now
    assert claims["exp"] > now
    # exp should be ~10 minutes from now
    assert claims["exp"] - now <= 660


async def test_get_installation_token_fresh(
    app_auth: GitHubAppAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh token fetch calls the GitHub API and returns the token."""
    call_count = 0

    async def mock_post(self, url, **kwargs):  # noqa: ANN001, ANN003
        nonlocal call_count
        call_count += 1

        class MockResponse:
            status_code = 201

            def json(self) -> dict:
                return {"token": "ghs_test_token_123"}

        return MockResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    token = await app_auth.get_installation_token(999)
    assert token == "ghs_test_token_123"
    assert call_count == 1


async def test_get_installation_token_cached(
    app_auth: GitHubAppAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second call uses cache, no additional HTTP request."""
    call_count = 0

    async def mock_post(self, url, **kwargs):  # noqa: ANN001, ANN003
        nonlocal call_count
        call_count += 1

        class MockResponse:
            status_code = 201

            def json(self) -> dict:
                return {"token": "ghs_cached_token"}

        return MockResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    token1 = await app_auth.get_installation_token(999)
    token2 = await app_auth.get_installation_token(999)
    assert token1 == token2 == "ghs_cached_token"
    assert call_count == 1


async def test_get_installation_token_expired(
    app_auth: GitHubAppAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired cache triggers a new API call."""
    call_count = 0

    async def mock_post(self, url, **kwargs):  # noqa: ANN001, ANN003
        nonlocal call_count
        call_count += 1

        class MockResponse:
            status_code = 201

            def json(self) -> dict:
                return {"token": f"ghs_token_{call_count}"}

        return MockResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    # First call
    await app_auth.get_installation_token(999)
    assert call_count == 1

    # Simulate expired cache (set expiry to past)
    app_auth._token_cache[999] = ("ghs_token_1", time.time() - 10)

    # Second call should fetch again
    token = await app_auth.get_installation_token(999)
    assert token == "ghs_token_2"
    assert call_count == 2


async def test_get_installation_token_http_error(
    app_auth: GitHubAppAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-201 response raises WebhookError."""

    async def mock_post(self, url, **kwargs):  # noqa: ANN001, ANN003
        class MockResponse:
            status_code = 401
            text = "Bad credentials"

        return MockResponse()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(WebhookError, match="401"):
        await app_auth.get_installation_token(999)
