"""Authentication middleware matching agent-gateway pattern.

Modes: dev-none, internal-only, cloudflare-access

cloudflare-access mode performs REAL Cloudflare Access JWT verification
(RS256 signature via JWKS fetched from
https://<team>.cloudflareaccess.com/cdn-cgi/access/certs, audience, issuer,
expiration) — mirrors agents_gateway.auth. The previous implementation only
checked for header presence, which permitted any string to bypass auth;
that footgun is now closed.
"""

import os
import secrets
from dataclasses import dataclass
from typing import Any

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

import jwt  # noqa: E402 — pyjwt pulled transitively via fastapi/fastmcp chain
from jwt import PyJWKClient, PyJWTError  # noqa: E402

import uuid  # noqa: E402 — already imported via uuid in other modules

logger = get_logger()


VALID_MODES = {"dev-none", "internal-only", "cloudflare-access"}

CF_JWT_HEADER = "Cf-Access-Jwt-Assertion"
INTERNAL_AUTH_HEADER = "X-Auth-Internal-Token"

PUBLIC_PATHS = {"/health", "/ready", "/version"}
OAUTH_PATHS = {"/.well-known/oauth-authorization-server", "/register", "/token"}


class AuthError(Exception):
    """Raised when the auth handler itself is misconfigured."""


@dataclass
class AuthResult:
    allowed: bool
    user: str = ""
    mode: str = ""
    error: str = ""


class AuthHandler:
    def __init__(self, config: AuthConfig) -> None:
        if config.mode not in VALID_MODES:
            raise ValueError(
                f"Invalid auth mode: {config.mode!r}. Valid: {sorted(VALID_MODES)}"
            )
        self.config = config
        self.mode = config.mode
        # JWKS client for Cloudflare Access (only constructed when needed).
        if config.mode == "cloudflare-access":
            if not config.cloudflare_team_domain:
                raise AuthError(
                    "auth.mode=cloudflare-access requires auth.cloudflare_team_domain "
                    "(env CONDUCTOR_AUTH__CLOUDFLARE_TEAM_DOMAIN)"
                )
            if not config.cloudflare_aud:
                raise AuthError(
                    "auth.mode=cloudflare-access requires auth.cloudflare_aud "
                    "(env CONDUCTOR_AUTH__CLOUDFLARE_AUD)"
                )
            team = config.cloudflare_team_domain.strip().rstrip("/")
            self._jwks_client: PyJWKClient | None = PyJWKClient(
                f"https://{team}/cdn-cgi/access/certs"
            )
            self._cf_issuer = f"https://{team}"
            self._cf_aud = config.cloudflare_aud
        else:
            self._jwks_client = None
            self._cf_issuer = ""
            self._cf_aud = ""

    def require_production_safe(self) -> None:
        if self.mode == "dev-none":
            env = os.environ.get("CONDUCTOR_ENVIRONMENT", os.environ.get("CONDUCTOR__ENVIRONMENT", ""))
            if env in ("production", "prod"):
                raise RuntimeError(
                    "dev-none auth mode is not allowed in production environment. "
                    "Set CONDUCTOR_ENVIRONMENT=dev or change auth mode."
                )
        if self.mode == "cloudflare-access" and (
            not self.config.cloudflare_team_domain or not self.config.cloudflare_aud
        ):
            raise RuntimeError(
                "auth.mode=cloudflare-access requires "
                "CONDUCTOR_AUTH__CLOUDFLARE_TEAM_DOMAIN and "
                "CONDUCTOR_AUTH__CLOUDFLARE_AUD to be configured."
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
            # Internal service-to-service bypass (Conductor↔Agents Gateway).
            # Same shared secret as agents_gateway — proves the caller is a
            # trusted in-cluster service and avoids the JWKS round-trip.
            if internal_token and self.config.internal_secret and secrets.compare_digest(internal_token, self.config.internal_secret):
                return AuthResult(allowed=True, user="internal", mode="internal-only bypass")
            if not cf_jwt:
                return AuthResult(allowed=False, mode="cloudflare-access", error=f"missing {CF_JWT_HEADER}")
            payload = _verify_cf_jwt(
                cf_jwt,
                self._jwks_client,
                self._cf_aud,
                self._cf_issuer,
                self.config.jwt_leeway_seconds,
            )
            if payload is None:
                return AuthResult(allowed=False, mode="cloudflare-access", error="invalid or expired Cloudflare Access JWT")
            email = payload.get("email") or payload.get("sub") or "cf-user"
            return AuthResult(allowed=True, user=email, mode="cloudflare-access")

        return AuthResult(allowed=False, mode=self.mode, error="unknown auth mode")


def _verify_cf_jwt(
    token: str,
    jwks_client: PyJWKClient | None,
    expected_aud: str,
    expected_issuer: str,
    leeway_seconds: int = 30,
) -> dict[str, Any] | None:
    """Verify a Cloudflare Access JWT.

    Required validation:
      * Signature is verified using JWKS from
        https://<team>.cloudflareaccess.com/cdn-cgi/access/certs
      * Algorithm MUST be RS256 (Cloudflare Access only ever issues RS256;
        this also rejects alg=none).
      * Audience MUST match expected_aud.
      * Issuer MUST match expected_issuer.
      * exp/nbf enforced by PyJWT with leeway_seconds slack.

    Returns verified claims dict on success, None on any failure.
    """
    if jwks_client is None:
        return None
    if not token or token.count(".") != 2:
        return None
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_aud,
            issuer=expected_issuer,
            leeway=leeway_seconds,
            options={"require": ["exp", "iss", "aud"]},
        )
    except PyJWTError:
        return None
    except Exception:
        # Defensive: any unexpected error (JWKS fetch failure, etc.) is an
        # auth failure, never a 500.
        return None
    if not isinstance(claims, dict):
        return None
    if "sub" not in claims and "email" not in claims:
        return None
    return claims


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