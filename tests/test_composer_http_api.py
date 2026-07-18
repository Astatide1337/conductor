"""Tests for Composer HTTP API endpoints."""

import pytest
from starlette.testclient import TestClient

from conductor.config import ConductorConfig
from conductor.server import create_app


@pytest.fixture
def app_client():
    cfg = ConductorConfig(environment="test")
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def composer_client(tmp_path):
    """Use FakeComposerLLMClient via tmp dir DB for deterministic isolated tests."""
    cfg = ConductorConfig(environment="test")
    cfg.storage.sqlite_path = str(tmp_path / "composer-api.db")
    # Enable composer without LLM API key -> use FakeComposerLLMClient
    cfg.composer.enabled = True
    cfg.composer.auto_start = True
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


class TestHealthVersion:
    def test_health_public(self, app_client):
        r = app_client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_version_public(self, app_client):
        r = app_client.get("/version")
        assert r.status_code == 200
        assert r.json()["service"] == "astatide-conductor"


class TestSubmitSpec:
    def test_submit_spec(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Build Composer v1",
            "spec": "Complete finalized specification text",
            "repository": {"url": "https://github.com/test/repo.git", "base_branch": "master"},
            "auto_start": True,
        })
        assert r.status_code in (200, 201)
        data = r.json()
        assert "objective_id" in data
        assert "composer_spec_id" in data
        assert data["status"] == "received"

    def test_submit_spec_autostart(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Build Feature",
            "spec": "Build a calculator with add, multiply, and divide",
            "auto_start": True,
        })
        assert r.status_code in (200, 201)
        data = r.json()
        assert data["status"] in ("received", "normalized", "planned", "executing")

    def test_submit_spec_no_autostart(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Build",
            "spec": "Spec text",
            "auto_start": False,
        })
        assert r.status_code in (200, 201)


class TestReadState:
    def test_list_objectives(self, composer_client):
        # First create an objective
        composer_client.post("/composer/objectives", json={
            "title": "Test obj",
            "spec": "spec text",
            "auto_start": False,
        })
        r = composer_client.get("/composer/objectives")
        assert r.status_code == 200
        data = r.json()
        assert "objectives" in data
        assert len(data["objectives"]) >= 1

    def test_get_objective(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Get test",
            "spec": "spec text",
            "auto_start": False,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.get(f"/composer/objectives/{obj_id}")
        assert r2.status_code == 200
        assert r2.json()["id"] == obj_id

    def test_get_objective_not_found(self, composer_client):
        r = composer_client.get("/composer/objectives/nonexistent")
        assert r.status_code == 404

    def test_get_spec(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Spec test",
            "spec": "spec text",
            "auto_start": False,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.get(f"/composer/objectives/{obj_id}/spec")
        assert r2.status_code == 200

    def test_get_plan(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Plan test",
            "spec": "spec text",
            "auto_start": True,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.get(f"/composer/objectives/{obj_id}/plan")
        assert r2.status_code in (200, 404)

    def test_get_tasks(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Tasks test",
            "spec": "spec text",
            "auto_start": True,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.get(f"/composer/objectives/{obj_id}/tasks")
        assert r2.status_code == 200
        data = r2.json()
        assert "tasks" in data

    def test_get_timeline(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Timeline test",
            "spec": "spec text",
            "auto_start": True,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.get(f"/composer/objectives/{obj_id}/timeline")
        assert r2.status_code == 200
        data = r2.json()
        assert "events" in data

    def test_get_report(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Report test",
            "spec": "spec text",
            "auto_start": True,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.get(f"/composer/objectives/{obj_id}/report")
        assert r2.status_code in (200, 404)


class TestControl:
    def test_start(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Start test",
            "spec": "spec text",
            "auto_start": False,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.post(f"/composer/objectives/{obj_id}/start")
        assert r2.status_code == 200

    def test_pause(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Pause test",
            "spec": "spec text",
            "auto_start": False,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.post(f"/composer/objectives/{obj_id}/pause")
        assert r2.status_code == 200

    def test_resume(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Resume test",
            "spec": "spec text",
            "auto_start": False,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.post(f"/composer/objectives/{obj_id}/resume")
        assert r2.status_code == 200

    def test_cancel(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Cancel test",
            "spec": "spec text",
            "auto_start": False,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.post(f"/composer/objectives/{obj_id}/cancel")
        assert r2.status_code == 200

    def test_reconcile(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Reconcile test",
            "spec": "spec text",
            "auto_start": True,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.post(f"/composer/objectives/{obj_id}/reconcile")
        assert r2.status_code == 200

    def test_steer(self, composer_client):
        r = composer_client.post("/composer/objectives", json={
            "title": "Steer test",
            "spec": "spec text",
            "auto_start": True,
        })
        obj_id = r.json()["objective_id"]
        r2 = composer_client.post(
            f"/composer/objectives/{obj_id}/steer",
            json={"guidance": "Keep API backward compatible"},
        )
        assert r2.status_code == 200
