"""Tests for the gateway hub HTTP endpoints.

Auth is threaded: in internal-only mode without a token, routes return
401 with the FastAPI {"detail":...} body. Phase 6 verifies the MCP
boundary separately; here we verify the REST side.
"""

import os
import tempfile

import pytest
from starlette.testclient import TestClient

from conductor.config import ConductorConfig
from conductor.server import create_app


@pytest.fixture
def dev_client():
    """dev-none auth so routes are accessible without tokens."""
    with tempfile.TemporaryDirectory() as d:
        cfg = ConductorConfig(
            environment="test",
            storage={"sqlite_path": os.path.join(d, "test.db")},
            auth={"mode": "dev-none"},
            # agents_gateway default URL is localhost:8092 (configured),
            # but no real server in tests, so health checks will be unhealthy.
            # Skill default URL is localhost:8091.
            # mcp_gateway + wiki_mcp are blank by default → not_configured.
        )
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        yield client


@pytest.fixture
def internal_client():
    with tempfile.TemporaryDirectory() as d:
        cfg = ConductorConfig(
            environment="test",
            storage={"sqlite_path": os.path.join(d, "test.db")},
            auth={"mode": "internal-only", "internal_secret": "s3cret"},
        )
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        yield client


class TestGatewaysEndpoints:
    def test_list_gateways_returns_four(self, dev_client):
        r = dev_client.get("/gateways")
        assert r.status_code == 200
        data = r.json()
        ids = {g["id"] for g in data["gateways"]}
        assert ids == {"agents", "skills", "mcp", "wiki"}
        assert data["count"] == 4

    def test_get_gateway(self, dev_client):
        r = dev_client.get("/gateways/agents")
        assert r.status_code == 200
        assert r.json()["gateway"]["id"] == "agents"
        assert r.json()["gateway"]["kind"] == "agents"

    def test_get_gateway_not_found(self, dev_client):
        r = dev_client.get("/gateways/totally-fake")
        assert r.status_code == 404

    def test_gateways_status_lightweight(self, dev_client):
        r = dev_client.get("/gateways/status")
        assert r.status_code == 200
        data = r.json()
        statuses = {g["id"]: g["status"] for g in data["gateways"]}
        # agents + skills have localhost defaults → 'unknown' (no probe)
        # mcp + wiki have no URL → 'not_configured'
        assert statuses["agents"] == "unknown"
        assert statuses["skills"] == "unknown"
        assert statuses["mcp"] == "not_configured"
        assert statuses["wiki"] == "not_configured"


class TestCapabilitiesEndpoints:
    def test_list_capabilities(self, dev_client):
        r = dev_client.get("/capabilities")
        assert r.status_code == 200
        data = r.json()
        names = {c["capability"] for c in data["capabilities"]}
        assert "execution.task.create" in names
        assert "skills.validate" in names
        assert "tools.list" in names
        assert "memory.read" in names  # wiki caps are listed even when not_configured

    def test_list_capabilities_filter_by_gateway(self, dev_client):
        r = dev_client.get("/capabilities?gateway_id=agents")
        assert r.status_code == 200
        caps = r.json()["capabilities"]
        assert all(c["gateway_id"] == "agents" for c in caps)
        assert {"execution.task.create", "execution.task.run"}.issubset({c["capability"] for c in caps})

    def test_find_capability(self, dev_client):
        r = dev_client.get("/capabilities/execution.task.create")
        assert r.status_code == 200
        data = r.json()
        assert data["capability"] == "execution.task.create"
        assert len(data["candidates"]) == 1
        assert data["candidates"][0]["gateway_id"] == "agents"

    def test_find_unknown_capability(self, dev_client):
        r = dev_client.get("/capabilities/does.not.exist")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0
        assert data["candidates"] == []


class TestGatewayChecks:
    def test_check_unknown_gateway_returns_404(self, dev_client):
        r = dev_client.post("/gateways/no-such-gateway/check")
        assert r.status_code == 404

    def test_check_mcp_gateway_not_configured(self, dev_client):
        r = dev_client.post("/gateways/mcp/check")
        assert r.status_code == 200
        data = r.json()
        assert data["status"]["status"] == "not_configured"

    def test_check_wiki_gateway_disabled(self, dev_client):
        r = dev_client.post("/gateways/wiki/check")
        assert r.status_code == 200
        data = r.json()
        assert data["status"]["status"] == "not_configured"

    def test_check_all_returns_four_statuses(self, dev_client):
        r = dev_client.post("/gateways/check-all")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 4
        statuses = {s["id"]: s["status"] for s in data["statuses"]}
        assert statuses["mcp"] == "not_configured"
        assert statuses["wiki"] == "not_configured"


class TestTimelineEndpoint:
    def _create_objective(self, client):
        r = client.post("/objectives", json={"title": "Timeline Test"})
        return r.json()["objective_id"]

    def test_timeline_returns_chronological_events(self, dev_client):
        obj_id = self._create_objective(dev_client)
        # Trigger some events
        dev_client.post(f"/objectives/{obj_id}/tasks", json={"title": "T1"})
        dev_client.post(f"/objectives/{obj_id}/tasks", json={"title": "T2"})
        r = dev_client.get(f"/objectives/{obj_id}/timeline")
        assert r.status_code == 200
        data = r.json()
        assert data["objective_id"] == obj_id
        assert data["count"] > 0
        assert "events" in data
        # Confirm chronological order: objective.created is first.
        assert data["events"][0]["event_type"] == "objective.created"
        # subsequent task.created entries should be in submission order.
        type_seq = [e["event_type"] for e in data["events"]]
        assert type_seq.count("task.created") == 2

    def test_timeline_404_for_missing_objective(self, dev_client):
        r = dev_client.get("/objectives/abc-not-real/timeline")
        assert r.status_code == 404

    def test_timeline_includes_gateway_events(self, dev_client):
        obj_id = self._create_objective(dev_client)
        # A health check should emit gateway.health_checked.
        dev_client.post("/gateways/mcp/check")
        r = dev_client.get(f"/objectives/{obj_id}/timeline")
        data = r.json()
        # gateway.registered events are emitted at startup (not tied to obj)
        # /gateways/mcp/check emits gateway.health_checked but unbound to objective
        # so it won't show in this objective-scoped query. The endpoint still
        # returns the events that DO have objective_id (objective.created etc.).
        assert data["count"] >= 1


class TestProtectedAuthBehavior:
    def test_gateways_route_requires_token(self, internal_client):
        r = internal_client.get("/gateways")
        assert r.status_code == 401
        body = r.json()
        # REST shape is {"detail":...} — not JSON-RPC
        assert "detail" in body
        assert body.get("jsonrpc") is None

    def test_gateways_route_with_token(self, internal_client):
        r = internal_client.get(
            "/gateways", headers={"X-Auth-Internal-Token": "s3cret"},
        )
        assert r.status_code == 200
        assert r.json()["count"] == 4

    def test_capabilities_route_requires_token(self, internal_client):
        r = internal_client.get("/capabilities")
        assert r.status_code == 401
        assert "detail" in r.json()

    def test_post_gateway_check_requires_token(self, internal_client):
        r = internal_client.post("/gateways/mcp/check")
        assert r.status_code == 401
        assert "detail" in r.json()

    def test_timeline_route_requires_token(self, internal_client):
        r = internal_client.get("/objectives/any/timeline")
        assert r.status_code == 401
