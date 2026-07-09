"""Authentication middleware matching agent-gateway pattern.

Modes: dev-none, internal-only, cloudflare-access
"""

import os
import secrets
from dataclasses import dataclass
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from conductor.config import AuthConfig
from conductor.logging import (
    SENSITIVE_HEADERS,
    auth_subject_var,
    bind_request_context,
    clear_request_context,
    get_logger,
    request_id_var,
)

import uuid  # noqa: E402 — already imported via uuid in other modules

logger = get_logger()


PUBLIC_PATHS = {"/health", "/ready", "/version"}
OAUTH_PATHS = {"/.well-known/oauth-authorization-server", "/register", "/token"}


@dataclass
class AuthResult:
    allowed: bool
    user: str = ""
    mode: str = ""
    error: str = ""


class AuthHandler:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self.mode = config.mode

    def require_production_safe(self) -> None:
        if self.mode == "dev-none":
            env = os.environ.get("CONDUCTOR_ENVIRONMENT", os.environ.get("CONDUCTOR__ENVIRONMENT", ""))
            if env in ("production", "prod"):
                raise RuntimeError(
                    "dev-none auth mode is not allowed in production environment. "
                    "Set CONDUCTOR_ENVIRONMENT=dev or change auth mode."
                )

    def check(
        self,
        client_host: str,
        internal_token: str | None = None,
        cf_jwt: str | None = None,
    ) -> AuthResult:
        if self.mode == "dev-none":
            return AuthResult(allowed=True, user="dev", mode="dev-none")

        if self.mode == "internal-only":
            if not self.config.internal_secret:
                return AuthResult(allowed=False, mode="internal-only", error="no internal secret configured")
            if internal_token and secrets.compare_digest(internal_token, self.config.internal_secret):
                return AuthResult(allowed=True, user="internal", mode="internal-only")
            return AuthResult(allowed=False, mode="internal-only", error="invalid or missing internal token")

        if self.mode == "cloudflare-access":
            if not self.config.cloudflare_team_domain:
                return AuthResult(allowed=False, mode="cloudflare-access", error="cloudflare not configured")
            if internal_token and self.config.internal_secret and secrets.compare_digest(internal_token, self.config.internal_secret):
                return AuthResult(allowed=True, user="internal", mode="internal-only bypass")
            if not cf_jwt:
                return AuthResult(allowed=False, mode="cloudflare-access", error="missing Cf-Access-Jwt-Assertion")
            return AuthResult(allowed=True, user="cf", mode="cloudflare-access")

        return AuthResult(allowed=False, mode=self.mode, error="unknown auth mode")


def _check_request_auth(auth_handler: AuthHandler, request: Request) -> AuthResult | None:
    """Run an auth check against the request. Returns None for public paths, else AuthResult."""
    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in OAUTH_PATHS):
        return None
    client_host = request.client.host if request.client else "unknown"
    internal_token = request.headers.get("X-Auth-Internal-Token")
    cf_jwt = request.headers.get("Cf-Access-Jwt-Assertion")
    return auth_handler.check(client_host, internal_token=internal_token, cf_jwt=cf_jwt)


def _make_auth_middleware_cls(auth_handler: AuthHandler):
    class _AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next) -> Response:
            rid = str(uuid.uuid4())
            request_id_var.set(rid)

            result = _check_request_auth(auth_handler, request)
            if result is None:
                try:
                    response = await call_next(request)
                    return response
                finally:
                    clear_request_context()

            if not result.allowed:
                clear_request_context()
                return JSONResponse(
                    {"detail": result.error or "Authentication required"},
                    status_code=401,
                )

            bind_request_context(rid, auth_subject=result.user)

            try:
                response = await call_next(request)
                return response
            finally:
                clear_request_context()

    return _AuthMiddleware


def make_mcp_auth_middleware_cls(auth_handler: AuthHandler):
    """Middleware for mounted MCP sub-apps. All MCP traffic requires auth — no public paths.

    Disallows unauthenticated initialize / tools/list / tool calls. The cockpit must
    identify itself the same way HTTP API callers do (internal token or CF JWT).
    """
    class _MCPAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next) -> Response:
            rid = str(uuid.uuid4())
            request_id_var.set(rid)

            client_host = request.client.host if request.client else "unknown"
            internal_token = request.headers.get("X-Auth-Internal-Token")
            cf_jwt = request.headers.get("Cf-Access-Jwt-Assertion")
            result = auth_handler.check(client_host, internal_token=internal_token, cf_jwt=cf_jwt)

            if not result.allowed:
                clear_request_context()
                return JSONResponse(
                    {"jsonrpc": "2.0", "error": {"code": -32001, "message": result.error or "Authentication required"}},
                    status_code=401,
                )

            bind_request_context(rid, auth_subject=result.user)
            try:
                response = await call_next(request)
                return response
            finally:
                clear_request_context()

    return _MCPAuthMiddleware