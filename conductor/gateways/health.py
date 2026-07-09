"""Gateway health checks — turn HTTP probe results into structured GatewayStatus.

Status rules (in order):

1. Missing required `base_url`     → `not_configured`
2. Gateway `enabled = False`       → `disabled`
3. /health 2xx                     → `healthy` (4xx other than 401/403 → unhealthy)
4. HTTP 401 / 403                  → `auth_failed`
5. `httpx.TimeoutException`        → `timeout`
6. Other 5xx                        → `unhealthy`
7. Unexpected exception            → `error`

`check_gateway_health` and `check_all_gateways` never raise — any failure
becomes a structured `GatewayStatus(status="..." , error="...")`. This
keeps the /gateways/check-all HTTP route from crashing a Conductor node
just because one downstream gateway is down.

Auth header construction is shared with the HTTP clients and uses the same
shape (`X-Auth-Internal-Token` for `internal-only`). We intentionally keep
CF Access out of health checks — Conductor never presents a CF JWT to a
downstream gateway it would manage.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx

from conductor.gateways.models import GatewayConfig, GatewayStatus
from conductor.gateways.registry import GatewayRegistry
from conductor.logging import get_logger

logger = get_logger()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _auth_headers(cfg: GatewayConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    if cfg.auth_mode == "internal-only":
        # Pull the token from the matching ConductorConfig subsection by id.
        # Health checks run on a config snapshot built once at registry
        # construction; conductor-gateway clients pass token via env at
        # creation, and we mirror that approach by reading env directly so
        # tests can mock with monkeypatch.
        import os
        env_map = {
            "agents": "CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN",
            "skills": "CONDUCTOR_SKILLS_GATEWAY_INTERNAL_TOKEN",
            "mcp": "CONDUCTOR_MCP_GATEWAY_INTERNAL_TOKEN",
            "wiki": "CONDUCTOR_WIKI_MCP_INTERNAL_TOKEN",
        }
        tok = os.environ.get(env_map.get(cfg.id, ""), "")
        if tok:
            headers["X-Auth-Internal-Token"] = tok
    return headers


def _capabilities_snapshot(registry: GatewayRegistry, cfg: GatewayConfig) -> list[str]:
    """Return the capability names for this gateway for inclusion in status."""
    from conductor.gateways.capabilities import capabilities_for_kind
    return capabilities_for_kind(cfg.kind)


def _probe(cfg: GatewayConfig) -> tuple[str | None, str | None, str, float | None]:
    """Synchronously probe one gateway. Return (version, error, status, latency_ms)."""
    base = cfg.base_url.rstrip("/")
    headers = _auth_headers(cfg)
    timeout = cfg.timeout_seconds

    t0 = time.perf_counter()
    try:
        with httpx.Client(base_url=base, timeout=timeout) as client:
            health_resp = client.get(cfg.health_path, headers=headers)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        if health_resp.status_code in (401, 403):
            return None, f"auth rejected: HTTP {health_resp.status_code}", "auth_failed", latency_ms
        if health_resp.status_code >= 500:
            return None, f"unhealthy: HTTP {health_resp.status_code}", "unhealthy", latency_ms
        if not (200 <= health_resp.status_code < 300):
            return None, f"unhealthy: HTTP {health_resp.status_code}", "unhealthy", latency_ms

        # /health ok — try /version (best-effort)
        version: str | None = None
        try:
            with httpx.Client(base_url=base, timeout=timeout) as client:
                v_resp = client.get(cfg.version_path, headers=headers)
            if 200 <= v_resp.status_code < 300:
                try:
                    vd = v_resp.json()
                    version = vd.get("version") or vd.get("service_version") or v_resp.text[:200]
                except Exception:
                    version = v_resp.text[:200] or None
        except Exception as ve:
            logger.debug("gateway_version_probe_failed id=%s err=%s", cfg.id, ve)

        return version, None, "healthy", latency_ms

    except httpx.TimeoutException as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return None, f"timeout: {e}", "timeout", latency_ms
    except httpx.HTTPError as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return None, f"network error: {e}", "unhealthy", latency_ms
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return None, f"error: {e}", "error", latency_ms


def check_gateway_health(
    registry: GatewayRegistry,
    gateway_id: str,
) -> GatewayStatus | None:
    """Probe one gateway by id. Never raises."""
    cfg = registry.get(gateway_id)
    if cfg is None:
        return None

    # 1. Missing URL → not_configured
    if not cfg.base_url:
        return GatewayStatus(
            id=cfg.id, kind=cfg.kind, name=cfg.name,
            enabled=cfg.enabled, configured=False, healthy=False,
            status="not_configured", base_url_present=False,
            auth_mode=cfg.auth_mode, capabilities=_capabilities_snapshot(registry, cfg),
            last_checked_at=_now_iso(), latency_ms=None, error="no base_url configured",
        )

    # 2. Disabled → disabled
    if not cfg.enabled:
        return GatewayStatus(
            id=cfg.id, kind=cfg.kind, name=cfg.name,
            enabled=False, configured=True, healthy=False,
            status="disabled", base_url_present=True,
            auth_mode=cfg.auth_mode, capabilities=_capabilities_snapshot(registry, cfg),
            last_checked_at=_now_iso(), latency_ms=None, error="gateway disabled",
        )

    version, error, status, latency = _safe_probe(cfg)
    healthy = status == "healthy"

    return GatewayStatus(
        id=cfg.id, kind=cfg.kind, name=cfg.name,
        enabled=True, configured=True, healthy=healthy,
        status=status, base_url_present=True,
        auth_mode=cfg.auth_mode,
        capabilities=_capabilities_snapshot(registry, cfg),
        last_checked_at=_now_iso(), latency_ms=latency, error=error,
        version=version,
    )


def _safe_probe(cfg: GatewayConfig) -> tuple[str | None, str | None, str, float | None]:
    """Probe one gateway, catching all unexpected exceptions and converting
    them to a structured (error, "error") status. Never raises.
    """
    try:
        return _probe(cfg)
    except Exception as e:
        return None, f"error: {e}", "error", None


def check_all_gateways(registry: GatewayRegistry) -> list[GatewayStatus]:
    """Probe every gateway in the registry. Returns one GatewayStatus per gateway."""
    return [
        s for s in (check_gateway_health(registry, g.id) for g in registry.all()) if s is not None
    ]
