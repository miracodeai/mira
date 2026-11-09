"""OpenID Connect single sign-on for the dashboard.

Authorization code flow with PKCE against any OIDC provider (Entra ID, Google,
Okta, Keycloak, Forgejo, ...). A successful sign-in creates the same session as
the password login, so the rest of the dashboard is unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from mira.dashboard.auth import SESSION_COOKIE
from mira.dashboard.db import SESSION_DURATION, AppDatabase, User

logger = logging.getLogger(__name__)

STATE_COOKIE = "mira_oidc_state"
STATE_TTL_SECONDS = 600
METADATA_TTL_SECONDS = 3600
CLOCK_LEEWAY_SECONDS = 60
_CALLBACK_COOKIE_PATH = "/api/auth/oidc"
_ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


class OIDCConfigError(ValueError):
    """MIRA_OIDC_* settings are incomplete."""


class SignInError(Exception):
    """A sign-in attempt was rejected. `code` is shown to the user, the message is only logged."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _csv(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class OIDCSettings:
    issuer: str
    client_id: str
    client_secret: str
    redirect_url: str
    dashboard_url: str = ""
    label: str = "SSO"
    scopes: str = "openid email profile"
    allowed_domains: frozenset[str] = frozenset()
    allowed_groups: frozenset[str] = frozenset()
    admin_groups: frozenset[str] = frozenset()
    groups_claim: str = "groups"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> OIDCSettings | None:
        """Settings from MIRA_OIDC_*; None when single sign-on is not configured."""
        issuer = env.get("MIRA_OIDC_ISSUER", "").strip().rstrip("/")
        if not issuer:
            return None
        client_id = env.get("MIRA_OIDC_CLIENT_ID", "").strip()
        dashboard_url = env.get("MIRA_DASHBOARD_URL", "").strip().rstrip("/")
        redirect_url = env.get("MIRA_OIDC_REDIRECT_URL", "").strip()
        if not redirect_url and dashboard_url:
            redirect_url = f"{dashboard_url}/api/auth/oidc/callback"
        missing = [
            name
            for name, value in (
                ("MIRA_OIDC_CLIENT_ID", client_id),
                ("MIRA_OIDC_REDIRECT_URL (or MIRA_DASHBOARD_URL)", redirect_url),
            )
            if not value
        ]
        if missing:
            raise OIDCConfigError(f"MIRA_OIDC_ISSUER is set but {' and '.join(missing)} is not")
        scopes = env.get("MIRA_OIDC_SCOPES", "").split() or ["openid", "email", "profile"]
        if "openid" not in scopes:
            scopes.insert(0, "openid")
        return cls(
            issuer=issuer,
            client_id=client_id,
            client_secret=env.get("MIRA_OIDC_CLIENT_SECRET", "").strip(),
            redirect_url=redirect_url,
            dashboard_url=dashboard_url,
            label=env.get("MIRA_OIDC_LABEL", "").strip() or "SSO",
            scopes=" ".join(scopes),
            allowed_domains=frozenset(
                d.lower().lstrip("@") for d in _csv(env.get("MIRA_OIDC_ALLOWED_DOMAINS", ""))
            ),
            allowed_groups=_csv(env.get("MIRA_OIDC_ALLOWED_GROUPS", "")),
            admin_groups=_csv(env.get("MIRA_OIDC_ADMIN_GROUPS", "")),
            groups_claim=env.get("MIRA_OIDC_GROUPS_CLAIM", "").strip() or "groups",
        )


class OIDCProvider:
    """Discovery, code exchange and ID token verification for one issuer."""

    def __init__(self, settings: OIDCSettings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self._http = http or httpx.Client(timeout=10)
        self._metadata: dict[str, Any] | None = None
        self._metadata_fetched_at = 0.0
        self._jwks: jwt.PyJWKSet | None = None

    def metadata(self) -> dict[str, Any]:
        stale = time.monotonic() - self._metadata_fetched_at > METADATA_TTL_SECONDS
        if self._metadata is None or stale:
            resp = self._http.get(f"{self.settings.issuer}/.well-known/openid-configuration")
            resp.raise_for_status()
            metadata = resp.json()
            if str(metadata.get("issuer", "")).rstrip("/") != self.settings.issuer:
                raise SignInError(
                    "sso_failed",
                    f"discovery issuer {metadata.get('issuer')!r} is not {self.settings.issuer!r}",
                )
            self._metadata = metadata
            self._metadata_fetched_at = time.monotonic()
            self._jwks = None
        return self._metadata

    def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        endpoint = self.metadata()["authorization_endpoint"]
        params = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_url,
            "scope": self.settings.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}"

    def exchange_code(self, code: str, code_verifier: str) -> str:
        metadata = self.metadata()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_url,
            "client_id": self.settings.client_id,
            "code_verifier": code_verifier,
        }
        basic_auth: httpx.BasicAuth | None = None
        if self.settings.client_secret:
            methods = metadata.get("token_endpoint_auth_methods_supported") or []
            if "client_secret_post" in methods:
                data["client_secret"] = self.settings.client_secret
            else:
                basic_auth = httpx.BasicAuth(
                    quote(self.settings.client_id, safe=""),
                    quote(self.settings.client_secret, safe=""),
                )
        headers = {"Accept": "application/json"}
        if basic_auth is None:
            resp = self._http.post(metadata["token_endpoint"], data=data, headers=headers)
        else:
            resp = self._http.post(
                metadata["token_endpoint"], data=data, headers=headers, auth=basic_auth
            )
        if resp.status_code != 200:
            raise SignInError(
                "sso_failed", f"token endpoint answered {resp.status_code}: {resp.text[:200]}"
            )
        id_token = resp.json().get("id_token")
        if not id_token:
            raise SignInError("sso_failed", "token response has no id_token")
        return str(id_token)

    def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        metadata = self.metadata()
        header = jwt.get_unverified_header(id_token)
        algorithm = header.get("alg", "")
        offered = metadata.get("id_token_signing_alg_values_supported") or ["RS256"]
        if algorithm not in _ASYMMETRIC_ALGORITHMS or algorithm not in offered:
            raise SignInError(
                "sso_failed", f"ID token signed with unsupported algorithm {algorithm!r}"
            )
        claims: dict[str, Any] = jwt.decode(
            id_token,
            key=self._signing_key(header.get("kid")),
            algorithms=[algorithm],
            audience=self.settings.client_id,
            issuer=metadata["issuer"],
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SignInError("sso_failed", "ID token nonce does not match the sign-in request")
        audience = claims["aud"]
        several = isinstance(audience, list) and len(audience) > 1
        if several and claims.get("azp") != self.settings.client_id:
            raise SignInError("sso_failed", "ID token has several audiences and no matching azp")
        return claims

    def _signing_key(self, kid: str | None) -> Any:
        for refresh in (False, True):
            if self._jwks is None or refresh:
                resp = self._http.get(self.metadata()["jwks_uri"])
                resp.raise_for_status()
                self._jwks = jwt.PyJWKSet.from_dict(resp.json())
            for key in self._jwks.keys:
                if kid is None or key.key_id == kid:
                    return key.key
        raise SignInError("sso_failed", f"no signing key {kid!r} in the provider's JWKS")


@dataclass(frozen=True)
class Identity:
    external_id: str
    username: str
    groups: frozenset[str]


def identity_from_claims(
    settings: OIDCSettings, issuer: str, claims: Mapping[str, Any]
) -> Identity:
    """Map verified ID token claims to a Mira identity and apply the access rules."""
    email = str(claims.get("email") or "").strip().lower()
    username = email or str(claims.get("preferred_username") or "").strip()
    if not username:
        raise SignInError("sso_failed", "ID token has neither an email nor a preferred_username")
    raw_groups = claims.get(settings.groups_claim) or []
    if isinstance(raw_groups, str):
        raw_groups = [raw_groups]
    groups = frozenset(str(g) for g in raw_groups)

    if settings.allowed_domains:
        if not email or claims.get("email_verified") is False:
            raise SignInError("sso_denied", f"{username} has no verified email to check the domain")
        if email.rsplit("@", 1)[-1] not in settings.allowed_domains:
            raise SignInError("sso_denied", f"{email} is not in an allowed domain")
    if settings.allowed_groups and not groups & settings.allowed_groups:
        raise SignInError("sso_denied", f"{username} is not in an allowed group")
    return Identity(external_id=f"{issuer}|{claims['sub']}", username=username, groups=groups)


def provision_user(db: AppDatabase, settings: OIDCSettings, identity: Identity) -> User:
    """Find or create the Mira user for an identity, keeping its admin flag in sync with groups."""
    admin_from_groups = (
        bool(identity.groups & settings.admin_groups) if settings.admin_groups else None
    )
    user = db.get_user_by_external_id(identity.external_id)
    if user is None:
        if db.get_user_by_username(identity.username) is not None:
            raise SignInError(
                "sso_conflict", f"a Mira user named {identity.username!r} already exists"
            )
        return db.create_sso_user(identity.username, identity.external_id, bool(admin_from_groups))
    username = user.username
    if identity.username != username and db.get_user_by_username(identity.username) is None:
        username = identity.username
    is_admin = user.is_admin if admin_from_groups is None else admin_from_groups
    if (username, is_admin) != (user.username, user.is_admin):
        db.update_sso_user(user.id, username, is_admin)
        user.username, user.is_admin = username, is_admin
    return user


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _safe_next(path: str) -> str:
    """Only same-site paths, so the post-login redirect can't be pointed elsewhere."""
    parsed = urlparse(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or parsed.scheme
        or parsed.netloc
    ):
        return "/"
    return path


def create_oidc_router(
    db: AppDatabase, settings: OIDCSettings, provider: OIDCProvider | None = None
) -> APIRouter:
    oidc = provider or OIDCProvider(settings)
    router = APIRouter(prefix="/api/auth/oidc", tags=["auth"])
    https = settings.redirect_url.startswith("https://")

    def to_login(code: str) -> RedirectResponse:
        response = RedirectResponse(f"{settings.dashboard_url}/login?error={code}", status_code=302)
        response.delete_cookie(STATE_COOKIE, path=_CALLBACK_COOKIE_PATH)
        return response

    @router.get("/login")
    def login(next: str = "/") -> RedirectResponse:
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        try:
            url = oidc.authorization_url(state, nonce, _code_challenge(code_verifier))
        except (httpx.HTTPError, SignInError, KeyError, ValueError) as exc:
            logger.warning("SSO sign-in could not start: %s", exc)
            return to_login("sso_failed")
        db.save_oidc_state(state, nonce, code_verifier, _safe_next(next), STATE_TTL_SECONDS)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            STATE_COOKIE,
            state,
            max_age=STATE_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=https,
            path=_CALLBACK_COOKIE_PATH,
        )
        return response

    @router.get("/callback")
    def callback(
        request: Request, state: str = "", code: str = "", error: str = ""
    ) -> RedirectResponse:
        pending = db.pop_oidc_state(state) if state else None
        try:
            if error:
                description = request.query_params.get("error_description", "")
                raise SignInError("sso_failed", f"provider returned {error}: {description}")
            cookie_state = request.cookies.get(STATE_COOKIE, "")
            if (
                pending is None
                or not cookie_state
                or not secrets.compare_digest(state, cookie_state)
            ):
                raise SignInError("sso_expired", "unknown, expired or reused sign-in state")
            nonce, code_verifier, next_path = pending
            if not code:
                raise SignInError("sso_failed", "provider returned no authorization code")
            claims = oidc.verify_id_token(oidc.exchange_code(code, code_verifier), nonce)
            identity = identity_from_claims(settings, oidc.metadata()["issuer"], claims)
            user = provision_user(db, settings, identity)
        except SignInError as exc:
            logger.warning("SSO sign-in rejected (%s): %s", exc.code, exc)
            return to_login(exc.code)
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, ValueError) as exc:
            logger.warning("SSO sign-in failed: %s", exc)
            return to_login("sso_failed")

        db.record_login(user.id)
        response = RedirectResponse(f"{settings.dashboard_url}{next_path}", status_code=302)
        response.set_cookie(
            SESSION_COOKIE,
            db.create_session(user.id),
            httponly=True,
            samesite="lax",
            secure=https or request.url.scheme == "https",
            max_age=SESSION_DURATION,
        )
        response.delete_cookie(STATE_COOKIE, path=_CALLBACK_COOKIE_PATH)
        logger.info("SSO sign-in: %s", user.username)
        return response

    return router
