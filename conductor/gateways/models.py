"""Pydantic models for the Gateway Hub registry.

Kinds of gateways Conductor recognises downstream:

- agents   — task execution / workers / runtimes / artifacts
- skills   — reusable skills / methodology / agent instructions
- mcp      — external MCP tools / connectors / general tool routing
- wiki     — durable memory / project context / decision logs
- custom   — future / bespoke downstream services

Each gateway config is intentionally permissive about URLs and tokens so
operators can register development, staging, or production endpoints
without rebuilding Conductor.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

GatewayKind = Literal["agents", "skills", "mcp", "wiki", "custom"]
GATEWAY_KINDS: frozenset[str] = frozenset({"agents", "skills", "mcp", "wiki", "custom"})

# A gateway's status string is one of these. We keep the literal strings to
# help callers introspect without importing the model.
GATEWAY_STATUSES: tuple[str, ...] = (
    "unknown",
    "not_configured",
    "disabled",
    "healthy",
    "degraded",
    "unhealthy",
    "auth_failed",
    "timeout",
    "error",
)


class GatewayConfig(BaseModel):
    """Configuration for a single downstream gateway.

    `base_url` may be empty (`""`); that produces a `not_configured` status.
    `enabled=False` produces a `disabled` status regardless of URL.
    """

    id: str
    name: str
    kind: str
    base_url: str = ""
    enabled: bool = True
    auth_mode: str = "dev-none"
    health_path: str = "/health"
    version_path: str = "/version"
    capabilities_path: str | None = None
    timeout_seconds: float = 5.0
    metadata: dict = Field(default_factory=dict)


class GatewayStatus(BaseModel):
    """Status result for a single gateway after a health check.

    Only fields that are safe to expose to operators / cockpits are present.
    Never include tokens here.
    """

    id: str
    kind: str
    name: str
    enabled: bool
    configured: bool
    healthy: bool
    status: str
    base_url_present: bool
    auth_mode: str
    version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    last_checked_at: str = ""
    latency_ms: float | None = None
    error: str | None = None
