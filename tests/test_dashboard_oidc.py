"""OpenID Connect sign-in for the dashboard, against an in-process fake identity provider."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mira.dashboard import oidc
from mira.dashboard.auth import AuthMiddleware, create_auth_router, password_login_enabled
from mira.dashboard.db import AppDatabase
from mira.dashboard.oidc import (
    OIDCConfigError,
    OIDCProvider,
    OIDCSettings,
    create_oidc_router,
)

ISSUER = "https://idp.test"
CLIENT_ID = "mira-dashboard"
CLIENT_SECRET = secrets.token_urlsafe(32) + "~+/"
REDIRECT_URL = "http://testserver/api/auth/oidc/callback"


class FakeIdP:
    """Discovery, JWKS and token endpoints; the test plays the browser for /authorize."""

    def __init__(self) -> None:
        self.auth_methods: list[str] | None = None
        self.issuer = ISSUER
        self.client_auth: list[str] = []
        self._codes: dict[str, dict[str, Any]] = {}
        self.rotate_key()

    def rotate_key(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = f"key-{time.monotonic_ns()}"

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def issue_code(self, authorize_url: str, claims: dict[str, Any]) -> tuple[str, str]:
        params = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        code = f"code-{len(self._codes)}"
        self._codes[code] = {
            "claims": {"nonce": params["nonce"], **claims},
            "challenge": params["code_challenge"],
            "redirect_uri": params["redirect_uri"],
        }
        return code, params["state"]

    def id_token(self, claims: dict[str, Any], **overrides: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "user-1",
            "iat": now,
            "exp": now + 300,
            **claims,
            **overrides,
        }
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": self.kid})

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            metadata: dict[str, Any] = {
                "issuer": self.issuer,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
            }
            if self.auth_methods is not None:
                metadata["token_endpoint_auth_methods_supported"] = self.auth_methods
            return httpx.Response(200, json=metadata)
        if url == f"{ISSUER}/jwks":
            return httpx.Response(200, json=self.jwks())
        if url == f"{ISSUER}/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            pending = self._codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "")
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if (
                pending is None
                or challenge != pending["challenge"]
                or form.get("redirect_uri") != pending["redirect_uri"]
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            if "client_secret" in form:
                self.client_auth.append("post")
                assert form["client_secret"] == CLIENT_SECRET
            elif "authorization" in request.headers:
                self.client_auth.append("basic")
                assert request.headers["authorization"].startswith("Basic ")
            else:
                self.client_auth.append("none")
                assert form["client_id"] == CLIENT_ID
            return httpx.Response(200, json={"id_token": self.id_token(pending["claims"])})
        return httpx.Response(404)


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@contextmanager
def postgres_schema() -> Iterator[str]:
    """A private schema in the disposable database named by MIRA_TEST_POSTGRES_URL."""
    url = os.environ.get("MIRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("MIRA_TEST_POSTGRES_URL must name a disposable test database")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "dashboard_oidc_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["options"] = f"-csearch_path={schema}"
        try:
            yield urlunsplit(parts._replace(query=urlencode(query)))
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(params=["sqlite", "postgres"])
def db(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[AppDatabase]:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    if request.param == "sqlite":
        database = AppDatabase(url="", admin_password="admin")
        yield database
        database.close()
        return
    with postgres_schema() as url:
        database = AppDatabase(url=url, admin_password="admin")
        try:
            yield database
        finally:
            database.close()


def make_client(
    db: AppDatabase, idp: FakeIdP, password_login: bool = True, **overrides: Any
) -> TestClient:
    defaults: dict[str, Any] = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_url": REDIRECT_URL,
        "label": "Acme SSO",
    }
    settings = OIDCSettings(**{**defaults, **overrides})
    provider = OIDCProvider(settings, http=httpx.Client(transport=httpx.MockTransport(idp.handle)))
    app = FastAPI()
    app.add_middleware(AuthMiddleware, db=db)
    app.include_router(
        create_auth_router(db, sso_label=settings.label, password_login=password_login)
    )
    app.include_router(create_oidc_router(db, settings, provider=provider))
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(db: AppDatabase, idp: FakeIdP) -> TestClient:
    return make_client(db, idp)


def sign_in(
    client: TestClient, idp: FakeIdP, claims: dict[str, Any] | None = None, next: str = "/"
) -> httpx.Response:
    login = client.get("/api/auth/oidc/login", params={"next": next})
    assert login.status_code == 302
    code, state = idp.issue_code(
        login.headers["location"], claims or {"email": "Ada@Example.com", "email_verified": True}
    )
    return client.get("/api/auth/oidc/callback", params={"code": code, "state": state})


def login_error(response: httpx.Response) -> str | None:
    location = urlparse(response.headers["location"])
    if location.path != "/login":
        return None
    return parse_qs(location.query)["error"][0]


# ── Settings ──


def test_settings_absent_without_issuer() -> None:
    assert OIDCSettings.from_env({}) is None


def test_settings_require_client_id_and_redirect() -> None:
    with pytest.raises(OIDCConfigError, match="MIRA_OIDC_CLIENT_ID"):
        OIDCSettings.from_env({"MIRA_OIDC_ISSUER": ISSUER, "MIRA_DASHBOARD_URL": "https://m"})
    with pytest.raises(OIDCConfigError, match="MIRA_OIDC_REDIRECT_URL"):
        OIDCSettings.from_env({"MIRA_OIDC_ISSUER": ISSUER, "MIRA_OIDC_CLIENT_ID": "id"})


def test_settings_from_env() -> None:
    settings = OIDCSettings.from_env(
        {
            "MIRA_OIDC_ISSUER": f"{ISSUER}/",
            "MIRA_OIDC_CLIENT_ID": "id",
            "MIRA_DASHBOARD_URL": "https://mira.example.com/",
            "MIRA_OIDC_SCOPES": "email groups",
            "MIRA_OIDC_ALLOWED_DOMAINS": "Example.com, @corp.example",
            "MIRA_OIDC_ADMIN_GROUPS": "org:admins,",
        }
    )
    assert settings is not None
    assert settings.issuer == ISSUER
    assert settings.redirect_url == "https://mira.example.com/api/auth/oidc/callback"
    assert settings.scopes == "openid email groups"
    assert settings.allowed_domains == {"example.com", "corp.example"}
    assert settings.admin_groups == {"org:admins"}
    assert settings.label == "SSO"


# ── Endpoints and flow ──


def test_providers_is_public(client: TestClient) -> None:
    resp = client.get("/api/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {
        "password": True,
        "sso": {"label": "Acme SSO", "login_url": "/api/auth/oidc/login"},
    }


def test_providers_without_sso(db: AppDatabase) -> None:
    app = FastAPI()
    app.include_router(create_auth_router(db))
    assert TestClient(app).get("/api/auth/providers").json()["sso"] is None


def test_login_redirects_with_pkce_state_and_nonce(client: TestClient, db: AppDatabase) -> None:
    resp = client.get("/api/auth/oidc/login")
    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    params = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/authorize"
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == REDIRECT_URL
    assert params["scope"].split()[0] == "openid"
    assert params["code_challenge_method"] == "S256"
    assert params["nonce"] and params["state"]
    cookie = resp.headers["set-cookie"]
    assert "mira_oidc_state=" in cookie and "HttpOnly" in cookie and "Path=/api/auth/oidc" in cookie


def test_sign_in_creates_user_and_session(client: TestClient, idp: FakeIdP) -> None:
    resp = sign_in(client, idp, next="/repos")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/repos"
    assert "mira_session=" in resp.headers["set-cookie"]

    me = client.get("/api/auth/me").json()
    assert me["username"] == "ada@example.com"
    assert me["sso"] is True
    assert me["is_admin"] is False
    assert idp.client_auth == ["basic"]


def test_public_client_sends_no_secret(db: AppDatabase, idp: FakeIdP) -> None:
    client = make_client(db, idp, client_secret="")
    assert login_error(sign_in(client, idp)) is None
    assert idp.client_auth == ["none"]


def test_client_secret_post_when_advertised(client: TestClient, idp: FakeIdP) -> None:
    idp.auth_methods = ["client_secret_post", "client_secret_basic"]
    assert login_error(sign_in(client, idp)) is None
    assert idp.client_auth == ["post"]


def test_returning_user_keeps_account_and_admin_follows_groups(
    db: AppDatabase, idp: FakeIdP
) -> None:
    client = make_client(db, idp, admin_groups=frozenset({"mira-admins"}))
    claims = {"email": "ada@example.com", "groups": ["engineering", "mira-admins"]}
    sign_in(client, idp, claims)
    first = client.get("/api/auth/me").json()
    assert first["is_admin"] is True

    sign_in(client, idp, {**claims, "groups": ["engineering"]})
    second = client.get("/api/auth/me").json()
    assert second["id"] == first["id"]
    assert second["is_admin"] is False


def test_admin_flag_untouched_without_admin_groups(
    client: TestClient, db: AppDatabase, idp: FakeIdP
) -> None:
    sign_in(client, idp)
    user = db.get_user_by_username("ada@example.com")
    assert user is not None
    db.update_sso_user(user.id, user.username, True)
    sign_in(client, idp)
    assert client.get("/api/auth/me").json()["is_admin"] is True


def test_changed_email_renames_the_user(client: TestClient, idp: FakeIdP) -> None:
    sign_in(client, idp)
    first = client.get("/api/auth/me").json()
    sign_in(client, idp, {"email": "ada.lovelace@example.com"})
    second = client.get("/api/auth/me").json()
    assert second["id"] == first["id"]
    assert second["username"] == "ada.lovelace@example.com"


def test_preferred_username_when_no_email(client: TestClient, idp: FakeIdP) -> None:
    sign_in(client, idp, {"preferred_username": "ada"})
    assert client.get("/api/auth/me").json()["username"] == "ada"


# ── Rejections ──


def test_state_must_match_the_browser_cookie(client: TestClient, idp: FakeIdP) -> None:
    login = client.get("/api/auth/oidc/login")
    code, state = idp.issue_code(login.headers["location"], {"email": "ada@example.com"})
    client.cookies.clear()
    resp = client.get("/api/auth/oidc/callback", params={"code": code, "state": state})
    assert login_error(resp) == "sso_expired"
    assert "mira_session=" not in resp.headers.get("set-cookie", "")


def test_state_is_single_use(client: TestClient, idp: FakeIdP) -> None:
    login = client.get("/api/auth/oidc/login")
    code, state = idp.issue_code(login.headers["location"], {"email": "ada@example.com"})
    assert (
        login_error(client.get("/api/auth/oidc/callback", params={"code": code, "state": state}))
        is None
    )
    client.cookies.set("mira_oidc_state", state, path="/api/auth/oidc")
    replay = client.get("/api/auth/oidc/callback", params={"code": code, "state": state})
    assert login_error(replay) == "sso_expired"


def test_expired_state_is_rejected(
    client: TestClient, idp: FakeIdP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oidc, "STATE_TTL_SECONDS", -1)
    assert login_error(sign_in(client, idp)) == "sso_expired"


def test_provider_error_is_reported(client: TestClient) -> None:
    resp = client.get("/api/auth/oidc/callback", params={"error": "access_denied", "state": "x"})
    assert login_error(resp) == "sso_failed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"nonce": "not-the-one-we-sent"},
        {"aud": "another-app"},
        {"iss": "https://evil.test"},
        {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200},
        {"aud": [CLIENT_ID, "another-app"]},
    ],
    ids=["nonce", "audience", "issuer", "expired", "multi-audience-without-azp"],
)
def test_invalid_id_token_is_rejected(
    client: TestClient, idp: FakeIdP, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    original = idp.id_token
    monkeypatch.setattr(idp, "id_token", lambda claims: original(claims, **overrides))
    assert login_error(sign_in(client, idp)) == "sso_failed"
    assert client.get("/api/auth/me").status_code == 401


def test_token_signed_with_unknown_key_is_rejected(
    client: TestClient, idp: FakeIdP, monkeypatch: pytest.MonkeyPatch
) -> None:
    rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def forged(claims: dict[str, Any]) -> str:
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "user-1", "iat": now, "exp": now + 300}
        return jwt.encode({**payload, **claims}, rogue, algorithm="RS256", headers={"kid": idp.kid})

    monkeypatch.setattr(idp, "id_token", forged)
    assert login_error(sign_in(client, idp)) == "sso_failed"


def test_symmetric_token_is_rejected(
    client: TestClient, idp: FakeIdP, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hs256(claims: dict[str, Any]) -> str:
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "user-1", "iat": now, "exp": now + 300}
        return jwt.encode({**payload, **claims}, CLIENT_SECRET, algorithm="HS256")

    monkeypatch.setattr(idp, "id_token", hs256)
    assert login_error(sign_in(client, idp)) == "sso_failed"


def test_rotated_signing_key_is_fetched(client: TestClient, idp: FakeIdP) -> None:
    assert login_error(sign_in(client, idp)) is None
    idp.rotate_key()
    assert login_error(sign_in(client, idp)) is None


def test_discovery_issuer_mismatch_stops_sign_in(client: TestClient, idp: FakeIdP) -> None:
    idp.issuer = "https://other.test"
    resp = client.get("/api/auth/oidc/login")
    assert login_error(resp) == "sso_failed"


def test_bad_code_is_rejected(client: TestClient, idp: FakeIdP) -> None:
    login = client.get("/api/auth/oidc/login")
    _, state = idp.issue_code(login.headers["location"], {"email": "ada@example.com"})
    resp = client.get("/api/auth/oidc/callback", params={"code": "forged", "state": state})
    assert login_error(resp) == "sso_failed"


@pytest.mark.parametrize(
    "claims",
    [
        {"email": "someone@gmail.com", "email_verified": True},
        {"email": "ada@example.com", "email_verified": False},
        {"preferred_username": "ada@example.com"},
    ],
    ids=["other-domain", "unverified", "no-email"],
)
def test_domain_allowlist(db: AppDatabase, idp: FakeIdP, claims: dict[str, Any]) -> None:
    client = make_client(db, idp, allowed_domains=frozenset({"example.com"}))
    assert login_error(sign_in(client, idp, claims)) == "sso_denied"
    assert db.get_user_by_username(claims.get("email") or claims["preferred_username"]) is None


def test_group_allowlist(db: AppDatabase, idp: FakeIdP) -> None:
    client = make_client(db, idp, allowed_groups=frozenset({"engineering"}))
    assert (
        login_error(sign_in(client, idp, {"email": "a@x.co", "groups": ["other"]})) == "sso_denied"
    )
    assert login_error(sign_in(client, idp, {"email": "a@x.co", "groups": "engineering"})) is None


def test_existing_local_user_is_not_taken_over(
    client: TestClient, db: AppDatabase, idp: FakeIdP
) -> None:
    db.create_user("ada@example.com", "local-password")
    assert login_error(sign_in(client, idp)) == "sso_conflict"
    assert db.authenticate("ada@example.com", "local-password") is not None


@pytest.mark.parametrize(
    "next_path", ["//evil.test/x", "https://evil.test", "/\\evil.test", "repos", ""]
)
def test_redirect_after_sign_in_stays_on_site(
    client: TestClient, idp: FakeIdP, next_path: str
) -> None:
    assert sign_in(client, idp, next=next_path).headers["location"] == "/"


# ── Password sign-in switch ──


@pytest.mark.parametrize(
    ("value", "enabled"),
    [(None, True), ("true", True), ("false", False), ("0", False), ("off", False), ("No", False)],
)
def test_password_login_setting(value: str | None, enabled: bool) -> None:
    env = {} if value is None else {"MIRA_PASSWORD_LOGIN": value}
    assert password_login_enabled(env) is enabled


def test_password_login_off_leaves_only_sso(db: AppDatabase, idp: FakeIdP) -> None:
    client = make_client(db, idp, password_login=False)
    assert client.get("/api/auth/providers").json()["password"] is False
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 403
    assert "mira_session=" not in resp.headers.get("set-cookie", "")
    assert login_error(sign_in(client, idp)) is None
    assert client.get("/api/auth/me").json()["sso"] is True


# ── Passwords for SSO accounts ──


def test_sso_user_cannot_use_password_login(client: TestClient, idp: FakeIdP) -> None:
    sign_in(client, idp)
    client.cookies.clear()
    for password in ("!", "", "anything"):
        resp = client.post(
            "/api/auth/login", json={"username": "ada@example.com", "password": password}
        )
        assert resp.status_code == 401


def test_sso_user_has_no_password_to_change(client: TestClient, idp: FakeIdP) -> None:
    sign_in(client, idp)
    resp = client.post(
        "/api/auth/change-password", json={"current_password": "", "new_password": "new"}
    )
    assert resp.status_code == 400


def test_admin_cannot_set_password_for_sso_user(
    client: TestClient, db: AppDatabase, idp: FakeIdP
) -> None:
    sign_in(client, idp)
    sso_user = db.get_user_by_username("ada@example.com")
    assert sso_user is not None
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    resp = client.post(f"/api/auth/users/{sso_user.id}/password", json={"new_password": "x"})
    assert resp.status_code == 400
    users = {u["username"]: u for u in client.get("/api/auth/users").json()}
    assert users["ada@example.com"]["sso"] is True
    assert users["admin"]["sso"] is False


# ── Schema ──


def test_existing_database_gains_sso_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    path = tmp_path / "_app.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, "
        "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, "
        "theme TEXT NOT NULL DEFAULT 'dark', created_at REAL NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ('old', 'x', 1)")
    conn.commit()
    conn.close()

    db = AppDatabase(url=f"sqlite:///{path}", admin_password="admin")
    old = db.get_user_by_username("old")
    assert old is not None and old.auth_provider == "local"
    db.create_sso_user("new@example.com", f"{ISSUER}|1", is_admin=False)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_sso_user("other@example.com", f"{ISSUER}|1", is_admin=False)


def test_existing_postgres_database_gains_sso_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    with postgres_schema() as url:
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE users (id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
                "password_hash TEXT NOT NULL, is_admin BOOLEAN NOT NULL DEFAULT FALSE, "
                "theme TEXT NOT NULL DEFAULT 'dark', created_at TIMESTAMPTZ DEFAULT NOW(), "
                "last_login_at DOUBLE PRECISION NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES ('old', 'x', TRUE)"
            )

        db = AppDatabase(url=url, admin_password="admin")
        try:
            old = db.get_user_by_username("old")
            assert old is not None and old.auth_provider == "local"
            db.create_sso_user("new@example.com", f"{ISSUER}|1", is_admin=False)
            with pytest.raises(psycopg.errors.UniqueViolation):
                db.create_sso_user("other@example.com", f"{ISSUER}|1", is_admin=False)
        finally:
            db.close()
