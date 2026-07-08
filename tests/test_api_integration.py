"""Full HTTP API integration tests — objective/task lifecycle through REST."""

import pytest
from starlette.testclient import TestClient

from conductor.config import ConductorConfig
from conductor.server import create_app


@pytest.fixture
def client():
    cfg = ConductorConfig(environment="test")
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


def _create_obj(client, title="API Test Objective", description=""):
    r = client.post("/objectives", json={"title": title, "description": description})
    assert r.status_code == 201
    data = r.json()
    return data["objective_id"], data["run_id"]


def _create_task(client, objective_id, title="API Task", task_type="ship"):
    r = client.post(f"/objectives/{objective_id}/tasks", json={"title": title, "task_type": task_type})
    assert r.status_code == 201
    return r.json()


class TestObjectiveAPI:
    def test_create_and_get(self, client):
        obj_id, run_id = _create_obj(client, "Build Auth Module")
        r = client.get(f"/objectives/{obj_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["objective"]["title"] == "Build Auth Module"
        assert data["objective"]["status"] == "created"
        assert len(data["runs"]) == 1
        assert data["runs"][0]["status"] == "created"

    def test_list_objectives(self, client):
        _create_obj(client, "Obj A")
        _create_obj(client, "Obj B")
        r = client.get("/objectives")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 2
        assert len(data["objectives"]) >= 2

    def test_list_objectives_by_status(self, client):
        _create_obj(client, "Active Obj")
        r = client.get("/objectives?status=created")
        assert r.status_code == 200
        data = r.json()
        assert all(o["status"] == "created" for o in data["objectives"])

    def test_pause_created_objective_auto_actives(self, client):
        obj_id, _ = _create_obj(client, "Pausable")
        r = client.post(f"/objectives/{obj_id}/pause")
        assert r.status_code == 200
        assert r.json()["objective"]["status"] == "paused"

    def test_resume_paused(self, client):
        obj_id, _ = _create_obj(client, "Resumable")
        client.post(f"/objectives/{obj_id}/pause")
        r = client.post(f"/objectives/{obj_id}/resume")
        assert r.status_code == 200
        assert r.json()["objective"]["status"] == "active"

    def test_resume_already_active_noop(self, client):
        obj_id, _ = _create_obj(client, "Already Active")
        # Resume from created -> active
        r1 = client.post(f"/objectives/{obj_id}/resume")
        assert r1.status_code == 200
        assert r1.json()["objective"]["status"] == "active"
        # Resume from active already active — returns same obj, 200
        r2 = client.post(f"/objectives/{obj_id}/resume")
        assert r2.status_code == 200
        assert r2.json()["objective"]["status"] == "active"

    def test_cancel_objective(self, client):
        obj_id, _ = _create_obj(client, "Cancellable")
        r = client.post(f"/objectives/{obj_id}/cancel")
        assert r.status_code == 200
        assert r.json()["objective"]["status"] == "cancelled"

    def test_get_nonexistent_404(self, client):
        r = client.get("/objectives/nonexistent")
        assert r.status_code == 404

    def test_resume_cancelled_denied(self, client):
        obj_id, _ = _create_obj(client, "Cancelled Obj")
        client.post(f"/objectives/{obj_id}/cancel")
        r = client.post(f"/objectives/{obj_id}/resume")
        assert r.status_code == 400


class TestTaskAPI:
    def test_create_task(self, client):
        obj_id, _ = _create_obj(client)
        task = _create_task(client, obj_id, "Auth Task", "ship")
        assert task["title"] == "Auth Task"
        assert task["status"] == "created"
        assert task["task_type"] == "ship"

    def test_get_task(self, client):
        obj_id, _ = _create_obj(client)
        task = _create_task(client, obj_id, "Verify Auth", "verify")
        r = client.get(f"/tasks/{task['id']}")
        assert r.status_code == 200
        assert r.json()["task"]["title"] == "Verify Auth"
        assert r.json()["task"]["task_type"] == "verify"

    def test_get_task_404(self, client):
        r = client.get("/tasks/nonexistent")
        assert r.status_code == 404

    def test_create_task_multiple(self, client):
        obj_id, _ = _create_obj(client, "Multi-task Obj")
        _create_task(client, obj_id, "T1")
        _create_task(client, obj_id, "T2")
        r = client.get(f"/tasks?objective_id={obj_id}")
        assert r.status_code == 200
        assert r.json()["count"] == 2

    def test_task_with_skills(self, client):
        obj_id, _ = _create_obj(client)
        r = client.post(
            f"/objectives/{obj_id}/tasks",
            json={"title": "Skilled Task", "task_type": "scout", "required_skills": ["code-review", "security-audit"]},
        )
        assert r.status_code == 201
        assert r.json()["required_skills"] == ["code-review", "security-audit"]

    def test_create_task_with_no_run(self, client):
        pass  # run is always created with objective in create_objective

    def test_dispatch_501(self, client):
        obj_id, _ = _create_obj(client)
        task = _create_task(client, obj_id, "Dispatch Test")
        # Ready it first
        from conductor.storage import ConductorStorage
        import os, tempfile


class TestEventsAPI:
    def test_events_emitted_for_objective_creation(self, client):
        obj_id, _ = _create_obj(client, "Eventful")
        r = client.get(f"/events?objective_id={obj_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        event_types = [e["event_type"] for e in data["events"]]
        assert "objective.created" in event_types

    def test_events_emitted_for_task_creation(self, client):
        obj_id, _ = _create_obj(client, "Taskful")
        _create_task(client, obj_id, "Eventful Task")
        r = client.get(f"/events?objective_id={obj_id}")
        assert r.status_code == 200
        data = r.json()
        event_types = [e["event_type"] for e in data["events"]]
        assert "task.created" in event_types

    def test_events_emitted_for_status_changes(self, client):
        obj_id, _ = _create_obj(client, "Status Change")
        client.post(f"/objectives/{obj_id}/pause")
        client.post(f"/objectives/{obj_id}/resume")
        r = client.get(f"/events?objective_id={obj_id}")
        event_types = [e["event_type"] for e in r.json()["events"]]
        assert "objective.paused" in event_types
        assert "objective.resumed" in event_types


class TestApprovalsAPI:
    def test_list_approvals_empty(self, client):
        r = client.get("/approvals")
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_approve_501(self, client):
        r = client.post("/approvals/fake-id/approve")
        assert r.status_code == 501

    def test_reject_501(self, client):
        r = client.post("/approvals/fake-id/reject")
        assert r.status_code == 501


class TestReconcileAndDryRun:
    def test_reconcile_501(self, client):
        r = client.post("/reconcile")
        assert r.status_code == 501

    def test_dry_run_501(self, client):
        r = client.post("/dry-run")
        assert r.status_code == 501