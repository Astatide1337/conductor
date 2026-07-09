"""Tests for gateway health checks — status rule coverage.

Uses respx to mock HTTP transport so none of the status mapping is left
to live HTTP behaviour. Each rule in the spec gets a dedicated test:

- 2xx /health          → healthy (with version probe optional)
- 401 / 403            → auth_failed
- 5xx                  → unhealthy
- timeout              → timeout
- transport/exception  → unhealthy for HTTPError, 'error' for weird ones
- not_configured       → when base_url empty
- disabled             → enabled=False
- check-all            → returns one GatewayStatus per gateway
"""

from __future__ import annotations

import httpx
import pytest
import respx

from conductor.gateways.models import GatewayConfig
from conductor.gateways.registry import GatewayRegistry
from conductor.gateways.health import check_gateway_health, check_all_gateways


def _reg(*gateways: GatewayConfig) -> GatewayRegistry:
    reg = GatewayRegistry()
    for g in gateways:
        reg.register(g)
    return reg


def _agents(url: str = "http://gw.test", enabled: bool = True,
            auth_mode: str = "internal-only") -> GatewayConfig:
    return GatewayConfig(
        id="agents", name="Agents Gateway", kind="agents", base_url=url,
        enabled=enabled, auth_mode=auth_mode,
        health_path="/health", version_path="/version",
        timeout_seconds=2.0,
    )


class TestHealthRules:
    @respx.mock
    def test_2xx_health_is_healthy(self):
        respx.get("http://gw.test/health").respond(200, json={"status": "ok"})
        respx.get("http://gw.test/version").respond(200, json={"version": "1.2"})
        st = check_gateway_health(_reg(_agents()), "agents")
        assert st.status == "healthy"
        assert st.healthy is True
        assert st.version == "1.2"
        assert st.latency_ms is not None
        assert st.latency_ms >= 0

    @respx.mock
    def test_401_maps_to_auth_failed(self):
        respx.get("http://gw.test/health").respond(401, json={"detail": "no"})
        st = check_gateway_health(_reg(_agents()), "agents")
        assert st.status == "auth_failed"
        assert st.healthy is False
        assert "401" in (st.error or "")

    @respx.mock
    def test_403_maps_to_auth_failed(self):
        respx.get("http://gw.test/health").respond(403)
        st = check_gateway_health(_reg(_agents()), "agents")
        assert st.status == "auth_failed"

    @respx.mock
    def test_5xx_maps_to_unhealthy(self):
        respx.get("http://gw.test/health").respond(503)
        st = check_gateway_health(_reg(_agents()), "agents")
        assert st.status == "unhealthy"
        assert st.healthy is False

    @respx.mock
    def test_timeout_maps_to_timeout(self):
        respx.get("http://gw.test/health").mock(side_effect=httpx.ConnectTimeout("slow"))
        st = check_gateway_health(_reg(_agents()), "agents")
        assert st.status == "timeout"
        assert st.healthy is False

    @respx.mock
    def test_4xx_other_than_401_403_maps_to_unhealthy(self):
        respx.get("http://gw.test/health").respond(404)
        st = check_gateway_health(_reg(_agents()), "agents")
        assert st.status == "unhealthy"
        assert st.healthy is False

    @respx.mock
    def test_transport_error_maps_to_unhealthy(self):
        respx.get("http://gw.test/health").mock(side_effect=httpx.ConnectError("nope"))
        st = check_gateway_health(_reg(_agents()), "agents")
        assert st.status == "unhealthy"
        assert "network error" in (st.error or "")

    @respx.mock
    def test_unexpected_exception_maps_to_error(self):
        # The health probe catches all exceptions and surfaces them as `error`.
        # We force a weird error by patching _probe to throw a ValueError.
        from conductor.gateways import health

        def _explode(cfg):
            raise ValueError("weird")
        orig_probe = health._probe
        health._probe = _explode
        try:
            st = check_gateway_health(_reg(_agents()), "agents")
            assert st.status == "error"
            assert st.healthy is False
        finally:
            health._probe = orig_probe

    def test_missing_url_is_not_configured(self):
        st = check_gateway_health(_reg(_agents(url="")), "agents")
        assert st.status == "not_configured"
        assert st.configured is False
        assert st.base_url_present is False
        assert st.healthy is False

    def test_disabled_is_disabled(self):
        st = check_gateway_health(_reg(_agents(enabled=False)), "agents")
        assert st.status == "disabled"
        assert st.enabled is False
        assert st.healthy is False

    def test_unknown_gateway_returns_none(self):
        st = check_gateway_health(_reg(_agents()), "nope")
        assert st is None

    @respx.mock
    def test_check_all_returns_one_per_gateway(self):
        respx.get("http://agents/health").respond(200, json={"status": "ok"})
        respx.get("http://agents/version").respond(200, json={"version": "1"})
        respx.get("http://skills/health").respond(503)
        reg = _reg(
            _agents(url="http://agents"),
            GatewayConfig(id="skills", name="Skills", kind="skills",
                          base_url="http://skills", enabled=True),
        )
        statuses = check_all_gateways(reg)
        assert len(statuses) == 2
        by_id = {s.id: s.status for s in statuses}
        assert by_id["agents"] == "healthy"
        assert by_id["skills"] == "unhealthy"


class TestStatusPayload:
    @respx.mock
    def test_capabilities_included_in_status(self):
        respx.get("http://gw.test/health").respond(200, json={"status": "ok"})
        respx.get("http://gw.test/version").respond(200, json={"version": "1"})
        st = check_gateway_health(_reg(_agents()), "agents")
        assert st.capabilities  # non-empty list of dotted caps
        assert "execution.task.create" in st.capabilities

    @respx.mock
    def test_last_checked_at_populated(self):
        respx.get("http://gw.test/health").respond(200)
        respx.get("http://gw.test/version").respond(200)
        st = check_gateway_health(_reg(_agents()), "agents")
        assert st.last_checked_at
        assert "T" in st.last_checked_at  # ISO format
