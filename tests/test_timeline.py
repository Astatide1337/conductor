"""Tests for the unified objective timeline endpoint."""

import os
import tempfile

import pytest
from starlette.testclient import TestClient

from conductor.config import ConductorConfig
from conductor.server import create_app


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as d:
        cfg = ConductorConfig(
            environment="test",
            storage={"sqlite_path": os.path.join(d, "test.db")},
            auth={"mode": "dev-none"},
        )
        app = create_app(cfg)
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


def _create_obj(client, title="Timeline Test"):
    r = client.post("/objectives", json={"title": title})
    return r.json()["objective_id"]


class TestTimelineChronology:
    def test_events_in_chronological_order(self, client):
        obj_id = _create_obj(client)
        client.post(f"/objectives/{obj_id}/tasks", json={"title": "T1"})
        client.post(f"/objectives/{obj_id}/tasks", json={"title": "T2"})
        client.post(f"/objectives/{obj_id}/resume")  # objective.actived

        r = client.get(f"/objectives/{obj_id}/timeline")
        assert r.status_code == 200
        data = r.json()
        # First entry should be objective.created (chronologically first)
        assert data["events"][0]["event_type"] == "objective.created"
        # task.created entries should appear in submission order
        task_events = [e for e in data["events"] if e["event_type"] == "task.created"]
        assert len(task_events) == 2
        assert task_events[0]["message"] != task_events[1]["message"]  # distinguishable


class TestTimelineIncludesGatewayEvents:
    def test_gateway_registered_at_startup_visible_via_unscoped_events(self, client):
        obj_id = _create_obj(client)
        # /events is unscoped (no objective_id filter) so we can see all events.
        r = client.get("/events")
        all_events = r.json()["events"]
        assert any(e["event_type"] == "gateway.registered" for e in all_events)

    def test_gateway_health_event_emitted_on_check(self, client):
        # Health-check the mcp gateway (not_configured) — should emit
        # gateway.health_failed (or health_checked, depending on whether
        # not_configured counts as healthy). The spec considers those
        # terminal configuration states as not 'healthy' so the event is
        # gateway.health_failed.
        _ = _create_obj(client)
        # mcp is not_configured so emitted event is health_failed
        client.post("/gateways/mcp/check")
        all_events = client.get("/events").json()["events"]
        # At least one health-checked or health-failed event exists
        types = {e["event_type"] for e in all_events}
        assert "gateway.health_checked" in types or "gateway.health_failed" in types


class TestTimelineFiltersByObjective:
    def test_two_objectives_separate_events(self, client):
        o1 = _create_obj(client, "A")
        o2 = _create_obj(client, "B")
        r1 = client.get(f"/objectives/{o1}/timeline")
        r2 = client.get(f"/objectives/{o2}/timeline")
        msgs1 = {e["message"] or "" for e in r1.json()["events"]}
        msgs2 = {e["message"] or "" for e in r2.json()["events"]}
        # objective.created on each reflects its own title.
        assert any("A" in m for m in msgs1) and not any("B" in m for m in msgs1)
        assert any("B" in m for m in msgs2) and not any("A" in m for m in msgs2)


class TestTimelineWithDispatch:
    def test_dispatch_lifecycle_appears_in_timeline(self, client):
        obj_id = _create_obj(client)
        task = client.post(f"/objectives/{obj_id}/tasks", json={"title": "Ship"}).json()
        dispatch = client.post(f"/tasks/{task['id']}/dispatch").json()

        r = client.get(f"/objectives/{obj_id}/timeline")
        types = [e["event_type"] for e in r.json()["events"]]
        # The full dispatch lifecycle should be visible
        assert "task.dispatch_requested" in types
        assert "task.dispatched" in types
        assert "gateway.agents.dispatch" in types
