"""MCP auth boundary tests — /mcp must enforce the same security model as the HTTP API.

The cockpit cannot bypass auth by talking to MCP instead of REST.
"""

import json
import os
import tempfile

import pytest
from starlette.testclient import TestClient

from conductor.config import ConductorConfig
from conductor.server import create_app


def _mcp_initialize_body():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-cockpit", "version": "1.0"},
        },
    }


def _mcp_tools_list_body():
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _mcp_tool_call_body(name: str, args: dict | None = None):
    return {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    }


@pytest.fixture
def internal_app():
    with tempfile.TemporaryDirectory() as d:
        cfg = ConductorConfig(
            environment="test",
            storage={"sqlite_path": os.path.join(d, "test.db")},
            auth={"mode": "internal-only", "internal_secret": "s3cret"},
        )
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        yield client


@pytest.fixture
def dev_none_app():
    with tempfile.TemporaryDirectory() as d:
        cfg = ConductorConfig(
            environment="test",
            storage={"sqlite_path": os.path.join(d, "test.db")},
            auth={"mode": "dev-none"},
        )
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        yield client


class TestMCPAuthBoundary:
    def test_unauthenticated_initialize_rejected(self, internal_app):
        """No token on /mcp → 401 — initialize rejected."""
        r = internal_app.post("/mcp/", json=_mcp_initialize_body())
        assert r.status_code == 401
        body = r.json()
        # JSONRPC-style error so a cockpit gets a parseable rejection
        if "jsonrpc" in body:
            assert body["error"]["code"] == -32001
        else:
            assert "detail" in body

    def test_authenticated_initialize_succeeds(self, internal_app):
        """Internal token on /mcp → initialize proceeds (200/406/etc, not 401)."""
        r = internal_app.post(
            "/mcp/",
            json=_mcp_initialize_body(),
            headers={"X-Auth-Internal-Token": "s3cret"},
        )
        assert r.status_code != 401, f"auth blocked despite valid token: {r.text}"

    def test_tools_list_requires_auth(self, internal_app):
        """No token → tools/list rejected with 401."""
        r = internal_app.post("/mcp/", json=_mcp_tools_list_body())
        assert r.status_code == 401

    def test_tool_call_requires_auth(self, internal_app):
        """No token → tools/call rejected with 401."""
        r = internal_app.post(
            "/mcp/",
            json=_mcp_tool_call_body("conductor_list_objectives"),
        )
        assert r.status_code == 401

    def test_wrong_token_rejected(self, internal_app):
        """Present but wrong token → 401, not silently allowed."""
        r = internal_app.post(
            "/mcp/",
            json=_mcp_initialize_body(),
            headers={"X-Auth-Internal-Token": "wrong"},
        )
        assert r.status_code == 401

    def test_dev_none_initialize_not_401(self, dev_none_app):
        """In dev-none, initialize must not be auth-blocked."""
        r = dev_none_app.post("/mcp/", json=_mcp_initialize_body())
        assert r.status_code != 401


class TestMCPAuthBoundaryAcceptsHTTPAPI:
    """Regression: confirm HTTP API still enforces auth identically (no regression)."""

    def test_http_api_protected_without_token(self, internal_app):
        r = internal_app.get("/objectives")
        assert r.status_code == 401

    def test_http_api_protected_with_token(self, internal_app):
        r = internal_app.get("/objectives", headers={"X-Auth-Internal-Token": "s3cret"})
        assert r.status_code != 401
