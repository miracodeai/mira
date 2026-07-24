"""Authentication routes and middleware for the dashboard API."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from mira.dashboard.db import AppDatabase

logger = logging.getLogger(__name__)

SESSION_COOKIE = "mira_session"

# Routes that don't require auth
_PUBLIC_PATHS = {"/api/auth/login", "/docs", "/openapi.json", "/redoc"}
_PUBLIC_PREFIXES = ("/api/repos/",)  # SVG endpoints need to be public for GitHub image embedding


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    is_admin: bool
    theme: str = "dark"
    last_login_at: float = 0


class SetThemeRequest(BaseModel):
    theme: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    new_password: str


def create_auth_router(db: AppDatabase) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/login")
    def login(body: LoginRequest, response: Response) -> dict:
        user = db.authenticate(body.username, body.password)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Invalid credentials"})
        db.record_login(user.id)
        token = db.create_session(user.id)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            max_age=86400 * 7,
        )
        return {
            "ok": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "is_admin": user.is_admin,
                "theme": user.theme,
            },
        }

    @router.post("/logout")
    def logout(request: Request, response: Response) -> dict:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            db.delete_session(token)
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @router.get("/me", response_model=UserResponse)
    def me(request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        return {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "theme": user.theme,
        }

    @router.put("/theme")
    def set_theme(body: SetThemeRequest, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        if body.theme not in ("dark", "light"):
            return JSONResponse(
                status_code=400, content={"error": "Theme must be 'dark' or 'light'"}
            )
        db.set_user_theme(user.id, body.theme)
        return {"ok": True, "theme": body.theme}

    @router.post("/change-password")
    def change_password(body: ChangePasswordRequest, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})
        if not body.new_password:
            return JSONResponse(status_code=400, content={"error": "New password cannot be empty"})
        # Re-verify the current password before allowing a change.
        if db.authenticate(user.username, body.current_password) is None:
            return JSONResponse(status_code=400, content={"error": "Current password is incorrect"})
        db.update_password(user.id, body.new_password)
        return {"ok": True}

    # ── User management (admin only) ──

    @router.get("/users", response_model=list[UserResponse])
    def list_users(request: Request) -> list[dict]:
        user = getattr(request.state, "user", None)
        if not user or not user.is_admin:
            return JSONResponse(status_code=403, content={"error": "Admin access required"})
        return [
            {
                "id": u.id,
                "username": u.username,
                "is_admin": u.is_admin,
                "last_login_at": u.last_login_at,
            }
            for u in db.list_users()
        ]

    @router.post("/users", response_model=UserResponse)
    def create_user(body: CreateUserRequest, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user or not user.is_admin:
            return JSONResponse(status_code=403, content={"error": "Admin access required"})
        try:
            new_user = db.create_user(body.username, body.password, is_admin=body.is_admin)
            return {"id": new_user.id, "username": new_user.username, "is_admin": new_user.is_admin}
        except Exception as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    @router.delete("/users/{user_id}")
    def delete_user(user_id: int, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user or not user.is_admin:
            return JSONResponse(status_code=403, content={"error": "Admin access required"})
        if user_id == user.id:
            return JSONResponse(status_code=400, content={"error": "Cannot delete yourself"})
        db.delete_user(user_id)
        return {"ok": True}

    @router.post("/users/{user_id}/password")
    def reset_user_password(user_id: int, body: ResetPasswordRequest, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        if not user or not user.is_admin:
            return JSONResponse(status_code=403, content={"error": "Admin access required"})
        if not body.new_password:
            return JSONResponse(status_code=400, content={"error": "New password cannot be empty"})
        db.update_password(user_id, body.new_password)
        return {"ok": True}

    return router


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, db: AppDatabase) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.db = db

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        # Skip auth for public paths and OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        # Allow SVG endpoints for GitHub image embedding
        if request.url.path.endswith(".svg"):
            return await call_next(request)
        # Non-API paths (frontend assets) don't need auth
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return JSONResponse(status_code=401, content={"error": "Not authenticated"})

        user = self.db.validate_session(token)
        if not user:
            return JSONResponse(status_code=401, content={"error": "Session expired"})

        request.state.user = user
        return await call_next(request)
