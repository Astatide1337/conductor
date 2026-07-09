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


def _mcp_error_body(message: str) -> dict:
    """Canonical JSON-RPC 2.0 server-error response for unauthorized MCP traffic.

    Shaped so MCP cockpits can parse -32001 (a reserved pre-defined server-error
    range) instead of receiving an opaque FastAPI 401 detail.
    """
    return {"jsonrpc": "2.0", "error": {"code": -32001, "message": message}, "id": None}


def _make_auth_middleware_cls(auth_handler: AuthHandler, *, mcp_path: str = "/mcp"):
    """HTTP auth middleware shared by all routes including /mcp.

    Any request whose path lives under the MCP mount is auth-checked by the same
    rule as the rest of Conductor. On failure /mcp responses are reshaped into
    JSON-RPC 2.0 error envelopes so MCP clients see a parseable rejection, while
    other paths keep the standard FastAPI {"detail": ...} shape for tooling.
    """
    # Normalize trailing slash once: /mcp and /mcp/ both fall under the prefix.
    mcp_prefix = mcp_path.rstrip("/") + "/"

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
                path = request.url.path
                # /mcp/* -> JSON-RPC error envelope
                if path.rstrip("/") == mcp_path.rstrip("/") or path.startswith(mcp_prefix):
                    return JSONResponse(
                        _mcp_error_body(result.error or "Unauthorized"),
                        status_code=401,
                    )
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


def make_mcp_auth_middleware_cls(auth_handler: AuthHandler, *, mcp_path: str = "/mcp"):
    """Standalone MCP-only auth middleware (kept for defense in depth).

    Used on the sub-app when MCP is mounted; produces JSON-RPC errors on
    failure. The parent FastAPI middleware already enforces the same model, so
    this rarely fires in practice but protects against future routing changes.
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
                    _mcp_error_body(result.error or "Unauthorized"),
                    status_code=401,
                )

            bind_request_context(rid, auth_subject=result.user)
            try:
                response = await call_next(request)
                return response
            finally:
                clear_request_context()

    return _MCPAuthMiddleware