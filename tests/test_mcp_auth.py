"""MCP auth boundary tests — /mcp must enforce the same security model as the HTTP API.

The cockpit cannot bypass auth by talking to MCP instead of REST. All unauthenticated
errors are shaped as JSON-RPC 2.0 envelopes with code -32001 so MCP clients parse
the rejection cleanly.
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


def _assert_jsonrpc_unauthorized(body: dict) -> None:
    """Body must be a JSON-RPC 2.0 error envelope with code -32001."""
    assert body.get("jsonrpc") == "2.0", f"expected jsonrpc 2.0 envelope, got: {body}"
    assert "error" in body, f"expected error field, got: {body}"
    err = body["error"]
    assert err.get("code") == -32001, f"expected code -32001, got: {err}"
    assert "message" in err, f"expected message in error, got: {err}"
    # id should be null or echo back the request id
    assert "id" in body


class TestMCPAuthBoundary:
    def test_unauthenticated_initialize_rejected(self, internal_app):
        """No token on /mcp → 401 with JSON-RPC-shaped body."""
        r = internal_app.post("/mcp/", json=_mcp_initialize_body())
        assert r.status_code == 401
        _assert_jsonrpc_unauthorized(r.json())

    def test_authenticated_initialize_succeeds(self, internal_app):
        """Internal token on /mcp → initialize proceeds (200/406/etc, not 401)."""
        r = internal_app.post(
            "/mcp/",
            json=_mcp_initialize_body(),
            headers={"X-Auth-Internal-Token": "s3cret"},
        )
        assert r.status_code != 401, f"auth blocked despite valid token: {r.text}"

    def test_tools_list_requires_auth(self, internal_app):
        """No token → tools/list rejected with JSON-RPC 401."""
        r = internal_app.post("/mcp/", json=_mcp_tools_list_body())
        assert r.status_code == 401
        _assert_jsonrpc_unauthorized(r.json())

    def test_tool_call_requires_auth(self, internal_app):
        """No token → tools/call rejected with JSON-RPC 401."""
        r = internal_app.post(
            "/mcp/",
            json=_mcp_tool_call_body("conductor_list_objectives"),
        )
        assert r.status_code == 401
        _assert_jsonrpc_unauthorized(r.json())

    def test_wrong_token_rejected(self, internal_app):
        """Present but wrong token → JSON-RPC 401, not silently allowed."""
        r = internal_app.post(
            "/mcp/",
            json=_mcp_initialize_body(),
            headers={"X-Auth-Internal-Token": "wrong"},
        )
        assert r.status_code == 401
        _assert_jsonrpc_unauthorized(r.json())

    def test_error_message_carries_auth_failure_detail(self, internal_app):
        """The JSON-RPC error message should carry the underlying auth failure detail."""
        r = internal_app.post("/mcp/", json=_mcp_initialize_body())
        body = r.json()
        assert body["error"]["message"], "error message must be non-empty"

    def test_mcp_path_prefix_matches(self, internal_app):
        """Both /mcp and /mcp/ must be auth-checked."""
        r_no_slash = internal_app.post("/mcp", json=_mcp_initialize_body())
        assert r_no_slash.status_code == 401
        _assert_jsonrpc_unauthorized(r_no_slash.json())

    def test_dev_none_initialize_not_401(self, dev_none_app):
        """In dev-none, initialize must not be auth-blocked."""
        r = dev_none_app.post("/mcp/", json=_mcp_initialize_body())
        assert r.status_code != 401


class TestMCPAuthBoundaryAcceptsHTTPAPI:
    """Regression: confirm HTTP API still enforces auth identically (no regression)."""

    def test_http_api_protected_without_token(self, internal_app):
        """REST endpoints still get the FastAPI {detail:...} shape, NOT JSON-RPC."""
        r = internal_app.get("/objectives")
        assert r.status_code == 401
        # HTTP API responses must NOT be JSON-RPC shaped — toolchain depends on it
        assert r.json().get("jsonrpc") != "2.0"
        assert "detail" in r.json()

    def test_http_api_protected_with_token(self, internal_app):
        r = internal_app.get("/objectives", headers={"X-Auth-Internal-Token": "s3cret"})
        assert r.status_code != 401

    def test_health_remains_public(self, internal_app):
        """/health is in PUBLIC_PATHS — no auth, never 401."""
        r = internal_app.get("/health")
        assert r.status_code == 200
