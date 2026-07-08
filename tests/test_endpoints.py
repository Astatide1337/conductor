"""Tests for public endpoints (health, ready, version)."""

import pytest
from starlette.testclient import TestClient

from conductor.config import ConductorConfig
from conductor.server import create_app


@pytest.fixture
def app_client():
    cfg = ConductorConfig(environment="test")
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


class TestPublicEndpoints:
    def test_health(self, app_client):
        r = app_client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["service"] == "astatide-conductor"

    def test_ready(self, app_client):
        r = app_client.get("/ready")
        assert r.status_code == 200  # DB initialized in create_app
        data = r.json()
        assert data["ready"] is True
        assert data["checks"]["storage"] == "ok"

    def test_version(self, app_client):
        r = app_client.get("/version")
        assert r.status_code == 200
        data = r.json()
        assert data["service"] == "astatide-conductor"
        assert "version" in data


class TestProtectedEndpoints:
    def test_protected_route_200_in_dev_none(self, app_client):
        r = app_client.get("/objectives")
        assert r.status_code == 501  # not implemented yet, but auth passes

    def test_metrics_endpoint(self, app_client):
        r = app_client.get("/metrics")
        assert r.status_code == 200
        assert "conductor_" in r.text or "metrics" in r.text.lower()