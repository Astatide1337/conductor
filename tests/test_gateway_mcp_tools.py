"""Tests for the MCP gateway hub tools — registration + tools/call surface.

Tools registered:
  conductor_list_gateways
  conductor_get_gateway_status
  conductor_check_gateway_health
  conductor_check_all_gateways
  conductor_list_capabilities
  conductor_find_capability
  conductor_call_mcp_gateway_tool (experimental)
  conductor_get_timeline

Plus the existing auth boundary: unauthenticated MCP returns JSON-RPC 401.
"""

import json
import os
import tempfile

import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient

from conductor.config import ConductorConfig
from conductor.circuit import BreakerEvaluator
from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.clients.mcp_gateway import MockMcpGatewayClient
from conductor.gateways import build_default_registry
from conductor.mcp_tools import register_conductor_tools
from conductor.server import create_app
from conductor.storage import ConductorStorage


@pytest.fixture
def mcp_state():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "test.db")
        storage = ConductorStorage(db)
        storage.initialize()
        cfg = ConductorConfig(environment="test", storage={"sqlite_path": db})
        gw = MockAgentsGatewayClient()
        gw.register_agent("code-validator", "Code Validator")
        mcp_gw = MockMcpGatewayClient()
        breaker = BreakerEvaluator(storage)
        reg = build_default_registry(cfg)
        mcp = FastMCP("Test Conductor")
        register_conductor_tools(
            mcp, cfg, storage, breaker, None, gw,
            gateway_registry=reg, mcp_gateway_client=mcp_gw,
        )
        yield mcp, storage, reg, mcp_gw


def _call(mcp, name, **kwargs):
    tool = mcp._tool_manager._tools.get(name)
    if not tool:
        raise ValueError(f"Tool {name} not found")
    return tool.fn(**kwargs)


class TestGatewayToolsRegistered:
    def test_all_gateway_tools_registered(self, mcp_state):
        mcp, storage, reg, mcp_gw = mcp_state
        names = {t.name for t in mcp._tool_manager._tools.values()}
        expected = {
            "conductor_list_gateways",
            "conductor_get_gateway_status",
            "conductor_check_gateway_health",
            "conductor_check_all_gateways",
            "conductor_list_capabilities",
            "conductor_find_capability",
            "conductor_call_mcp_gateway_tool",
            "conductor_get_timeline",
        }
        missing = expected - names
        assert not missing, f"Missing gateway tools: {missing}"


class TestListGateways:
    async def test_list_gateways(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _call(mcp, "conductor_list_gateways")
        data = json.loads(result)
        ids = {g["id"] for g in data["gateways"]}
        assert ids == {"agents", "skills", "mcp", "wiki"}
        assert data["count"] == 4


class TestGatewayStatus:
    async def test_get_status_unknown(self, mcp_state):
        mcp, *_ = mcp_state
        # agents has default localhost URL — should be 'unknown'
        result = await _call(mcp, "conductor_get_gateway_status", gateway_id="agents")
        data = json.loads(result)
        assert data["status"] == "unknown"

    async def test_get_status_not_configured(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _call(mcp, "conductor_get_gateway_status", gateway_id="mcp")
        data = json.loads(result)
        assert data["status"] == "not_configured"

    async def test_get_status_not_found(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _call(mcp, "conductor_get_gateway_status", gateway_id="nope")
        data = json.loads(result)
        assert "error" in data


class TestCheckHealth:
    async def test_check_mcp_unconfigured(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _call(mcp, "conductor_check_gateway_health", gateway_id="mcp")
        data = json.loads(result)
        assert data["status"] == "not_configured"

    async def test_check_all(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _call(mcp, "conductor_check_all_gateways")
        data = json.loads(result)
        # all 4 gateways returned
        ids = {s["id"] for s in data["statuses"]}
        assert ids == {"agents", "skills", "mcp", "wiki"}


class TestCapabilitiesMcp:
    async def test_list_capabilities(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _call(mcp, "conductor_list_capabilities")
        data = json.loads(result)
        names = {c["capability"] for c in data["capabilities"]}
        assert "execution.task.create" in names
        assert "memory.read" in names

    async def test_find_capability(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _call(mcp, "conductor_find_capability", capability="execution.task.create")
        data = json.loads(result)
        assert data["capability"] == "execution.task.create"
        assert len(data["candidates"]) == 1
        assert data["candidates"][0]["gateway_id"] == "agents"

    async def test_find_unknown_capability(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _call(mcp, "conductor_find_capability", capability="never.exists")
        data = json.loads(result)
        assert data["count"] == 0
        assert data["candidates"] == []


class TestCallMcpGatewayTool:
    async def test_call_mcp_gateway_tool_with_mock(self, mcp_state):
        mcp, storage, reg, mcp_gw = mcp_state
        # mcp_state set up the mock client. Call a known tool.
        result = await _call(mcp, "conductor_call_mcp_gateway_tool",
                             tool_name="github.search",
                             arguments_json='{"query":"test"}')
        data = json.loads(result)
        assert data["ok"] is True
        assert data["tool"] == "github.search"
        assert "result" in data

    async def test_call_mcp_gateway_tool_unknown(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _call(mcp, "conductor_call_mcp_gateway_tool",
                             tool_name="never.tool",
                             arguments_json="{}")
        data = json.loads(result)
        assert data["ok"] is False
        assert "error" in data


class TestGetTimeline:
    async def test_get_timeline_returns_chronological(self, mcp_state):
        mcp, storage, *_ = mcp_state
        # Create an objective + task via MCP to generate events
        await _call(mcp, "conductor_create_objective", title="Timeline MCP")
        events = await _call(mcp, "conductor_view_events")
        events_data = json.loads(events)
        # We have at least objective.created
        obj_id = None
        for e in events_data["events"]:
            if e["event_type"] == "objective.created":
                obj_id = e["objective_id"]
                break
        assert obj_id
        result = await _call(mcp, "conductor_get_timeline", objective_id=obj_id)
        data = json.loads(result)
        assert data["objective_id"] == obj_id
        assert data["count"] >= 1
        # First event must be objective.created (chronological)
        assert data["events"][0]["event_type"] == "objective.created"


class TestMCPAuthStillRejects:
    def test_unauthenticated_mcp_returns_jsonrpc_401(self, internal_app=None):
        """MCP unauthenticated boundary still rejects with JSON-RPC.
        This duplicates intent from test_mcp_auth.py but for the new tools.
        """
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = ConductorConfig(
                environment="test",
                storage={"sqlite_path": os.path.join(d, "test.db")},
                auth={"mode": "internal-only", "internal_secret": "s3cret"},
            )
            app = create_app(cfg)
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert r.status_code == 401
            body = r.json()
            assert body.get("jsonrpc") == "2.0"
            assert body["error"]["code"] == -32001
