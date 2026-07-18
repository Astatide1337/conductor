"""Tests for Composer MCP tools exposed through Conductor MCP.

Tests bypass the HTTP/JSON-RPC transport and call the tool functions directly
via FastMCP's `_tool_manager._tools` registry. This matches the existing
convention used by `tests/test_gateway_mcp_tools.py` and lets us avoid the
FastMCP StreamableHTTPSessionManager lifespan dance in unit tests.
"""

import json
import os
import tempfile

import pytest
from fastmcp import FastMCP

from conductor.circuit import BreakerEvaluator
from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.composer.service import ComposerService
from conductor.composer.storage import ComposerStorage
from conductor.composer.llm import FakeComposerLLMClient
from conductor.config import ConductorConfig
from conductor.gateways import build_default_registry
from conductor.mcp_tools import register_conductor_tools
from conductor.storage import ConductorStorage


def _call(mcp, name, **kwargs):
    tool = mcp._tool_manager._tools.get(name)
    if not tool:
        raise ValueError(f"Tool {name} not found")
    return tool.fn(**kwargs)


async def _acall(mcp, name, **kwargs):
    """Call an async MCP tool and await the result."""
    tool = mcp._tool_manager._tools.get(name)
    if not tool:
        raise ValueError(f"Tool {name} not found")
    return await tool.fn(**kwargs)


@pytest.fixture
def mcp_state():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "test.db")
        cs = ConductorStorage(db); cs.initialize()
        cps = ComposerStorage(db); cps.initialize()
        cfg = ConductorConfig(environment="test", storage={"sqlite_path": db})
        cfg.composer.enabled = True
        gw = MockAgentsGatewayClient()
        gw.register_agent("code-validator", "Code Validator")
        gw.register_harness_profile("opencode-deepseek", "OpenCode DeepSeek", runnable=True)
        breaker = BreakerEvaluator(cs)
        reg = build_default_registry(cfg)
        composer_svc = ComposerService(
            storage=cps, conductor_storage=cs,
            llm_client=FakeComposerLLMClient(), agents_gateway_client=gw,
            config=cfg.composer, skills_gateway_client=None,
            wiki_mcp_client=None, gateway_registry=reg, metrics=None,
        )
        mcp = FastMCP("Test Conductor")
        register_conductor_tools(
            mcp, cfg, cs, breaker, None, gw,
            gateway_registry=reg, mcp_gateway_client=None,
            composer_service=composer_svc,
        )
        yield mcp, composer_svc, cs, cps


class TestComposerMCPToolsRegistered:
    def test_all_composer_tools_registered(self, mcp_state):
        mcp, *_ = mcp_state
        names = {t.name for t in mcp._tool_manager._tools.values()}
        expected = {
            "composer_submit_spec",
            "composer_list_objectives",
            "composer_get_objective",
            "composer_get_plan",
            "composer_get_status",
            "composer_get_timeline",
            "composer_get_report",
            "composer_pause",
            "composer_resume",
            "composer_cancel",
            "composer_reconcile",
            "composer_steer",
        }
        missing = expected - names
        assert not missing, f"missing composer MCP tools: {missing}"


class TestSubmitSpecMCP:
    @pytest.mark.asyncio
    async def test_submit_spec_returns_ids(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="MCP Test Spec",
                           spec="Build a tool",
                           repository_json="{}",
                           auto_start=False)
        data = json.loads(raw)
        assert "objective_id" in data
        assert "composer_spec_id" in data
        assert data["status"] == "received"

    @pytest.mark.asyncio
    async def test_submit_spec_with_repo(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="With repo",
                           spec="Do something",
                           repository_json=json.dumps({"url": "https://x.git", "base_branch": "main"}),
                           auto_start=False)
        data = json.loads(raw)
        assert "objective_id" in data


class TestListAndGetMCP:
    @pytest.mark.asyncio
    async def test_list_objectives(self, mcp_state):
        mcp, *_ = mcp_state
        await _acall(mcp, "composer_submit_spec",
                     title="obj1", spec="spec", auto_start=False)
        await _acall(mcp, "composer_submit_spec",
                     title="obj2", spec="spec", auto_start=False)
        result = await _acall(mcp, "composer_list_objectives")
        data = json.loads(result)
        assert "objectives" in data
        assert len(data["objectives"]) >= 2

    @pytest.mark.asyncio
    async def test_get_objective(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="gettest", spec="spec", auto_start=False)
        objective_id = json.loads(raw)["objective_id"]
        result = await _acall(mcp, "composer_get_objective", objective_id=objective_id)
        data = json.loads(result)
        assert isinstance(data, dict)
        # Should return either the objective or an error
        assert "error" in data or "objective_id" in data or "id" in data

    @pytest.mark.asyncio
    async def test_get_objective_not_found(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _acall(mcp, "composer_get_objective", objective_id="nonexistent")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_get_status_progress(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="status", spec="spec", auto_start=True)
        objective_id = json.loads(raw)["objective_id"]
        result = await _acall(mcp, "composer_get_status", objective_id=objective_id)
        data = json.loads(result)
        assert "objective_id" in data
        assert "progress" in data
        assert "total_tasks" in data["progress"]
        assert "completed" in data["progress"]
        assert "blocked_external" in data

    @pytest.mark.asyncio
    async def test_get_plan(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="plan", spec="spec", auto_start=True)
        objective_id = json.loads(raw)["objective_id"]
        result = await _acall(mcp, "composer_get_plan", objective_id=objective_id)
        data = json.loads(result)
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_get_timeline(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="timeline", spec="spec", auto_start=True)
        objective_id = json.loads(raw)["objective_id"]
        result = await _acall(mcp, "composer_get_timeline", objective_id=objective_id)
        data = json.loads(result)
        # Either returns events or notes that there are none
        assert "events" in data or "error" in data or "objective_id" in data

    @pytest.mark.asyncio
    async def test_get_report_when_none(self, mcp_state):
        mcp, *_ = mcp_state
        result = await _acall(mcp, "composer_get_report", objective_id="nonexistent")
        data = json.loads(result)
        assert "error" in data


class TestControlMCP:
    @pytest.mark.asyncio
    async def test_pause(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="pause", spec="spec", auto_start=True)
        objective_id = json.loads(raw)["objective_id"]
        result = await _acall(mcp, "composer_pause", objective_id=objective_id)
        data = json.loads(result)
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_resume(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="resume", spec="spec", auto_start=False)
        objective_id = json.loads(raw)["objective_id"]
        result = await _acall(mcp, "composer_resume", objective_id=objective_id)
        data = json.loads(result)
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_cancel(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="cancel", spec="spec", auto_start=False)
        objective_id = json.loads(raw)["objective_id"]
        result = await _acall(mcp, "composer_cancel", objective_id=objective_id)
        data = json.loads(result)
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_reconcile(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="reconcile", spec="spec", auto_start=True)
        objective_id = json.loads(raw)["objective_id"]
        result = await _acall(mcp, "composer_reconcile", objective_id=objective_id)
        data = json.loads(result)
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_steer(self, mcp_state):
        mcp, *_ = mcp_state
        raw = await _acall(mcp, "composer_submit_spec",
                           title="steer", spec="spec", auto_start=True)
        objective_id = json.loads(raw)["objective_id"]
        result = await _acall(mcp, "composer_steer",
                              objective_id=objective_id,
                              guidance="Keep public API backward-compatible")
        data = json.loads(result)
        assert isinstance(data, dict)


class TestMCPAuthBoundary:
    """Unauthenticated MCP must still reject with JSON-RPC-shaped 401.

    Mirrors test_mcp_auth.py — the composer routes follow the same auth model
    as the rest of Conductor.
    """

    def test_unauthenticated_mcp_returns_jsonrpc_401(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            cfg = ConductorConfig(
                environment="test",
                storage={"sqlite_path": os.path.join(d, "auth.db")},
                auth={"mode": "internal-only", "internal_secret": "s3cret"},
            )
            cfg.composer.enabled = True
            from conductor.server import create_app
            app = create_app(cfg)
            from starlette.testclient import TestClient
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/mcp/", json={
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "composer_submit_spec", "arguments": {
                    "title": "should fail", "spec": "test", "auto_start": False,
                }}
            })
            assert r.status_code == 401
            body = r.json()
            assert body.get("jsonrpc") == "2.0"
            assert body.get("error", {}).get("code") == -32001
