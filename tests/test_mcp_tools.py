"""Tests for MCP cockpit tools — call through MCP server."""

import json
import os
import tempfile

import pytest
from fastmcp import FastMCP

from conductor.config import ConductorConfig
from conductor.storage import ConductorStorage
from conductor.circuit import BreakerEvaluator
from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.mcp_tools import register_conductor_tools


@pytest.fixture
def mcp_state():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        storage = ConductorStorage(db_path)
        storage.initialize()
        cfg = ConductorConfig(environment="test")
        gw = MockAgentsGatewayClient()
        gw.register_agent("code-validator", "Code Validator")
        breaker = BreakerEvaluator(storage)
        mcp = FastMCP("Test Conductor")
        register_conductor_tools(mcp, cfg, storage, breaker, None, gw)
        yield mcp, storage


def _call(mcp, name, **kwargs):
    """Call a registered MCP tool by name."""
    tool = mcp._tool_manager._tools.get(name)
    if not tool:
        raise ValueError(f"Tool {name} not found")
    return tool.fn(**kwargs)


class TestMCPToolsList:
    def test_all_tools_registered(self, mcp_state):
        mcp, storage = mcp_state
        tools = mcp._tool_manager._tools
        names = {t.name for t in tools.values()}
        expected = {
            "conductor_create_objective",
            "conductor_get_objective",
            "conductor_list_objectives",
            "conductor_get_status",
            "conductor_create_task",
            "conductor_dispatch_task",
            "conductor_list_approvals",
            "conductor_approve",
            "conductor_reject",
            "conductor_steer_objective",
            "conductor_pause_objective",
            "conductor_resume_objective",
            "conductor_cancel_objective",
            "conductor_dry_run",
            "conductor_health_check",
        }
        missing = expected - names
        assert not missing, f"Missing tools: {missing}"


class TestMCPToolCalls:
    async def test_create_objective(self, mcp_state):
        mcp, storage = mcp_state
        result = await _call(mcp, "conductor_create_objective", title="MCP Obj", description="Test")
        data = json.loads(result)
        assert "objective_id" in data

    async def test_list_objectives(self, mcp_state):
        mcp, storage = mcp_state
        await _call(mcp, "conductor_create_objective", title="A")
        await _call(mcp, "conductor_create_objective", title="B")
        result = await _call(mcp, "conductor_list_objectives", status="created")
        data = json.loads(result)
        assert data["count"] >= 2

    async def test_get_objective(self, mcp_state):
        mcp, storage = mcp_state
        created = await _call(mcp, "conductor_create_objective", title="Get Me")
        obj_id = json.loads(created)["objective_id"]
        result = await _call(mcp, "conductor_get_objective", objective_id=obj_id)
        data = json.loads(result)
        assert data["objective"]["title"] == "Get Me"

    async def test_health_check(self, mcp_state):
        mcp, storage = mcp_state
        result = await _call(mcp, "conductor_health_check")
        data = json.loads(result)
        assert data["status"] == "healthy"

    async def test_create_task(self, mcp_state):
        mcp, storage = mcp_state
        created = await _call(mcp, "conductor_create_objective", title="Task Parent")
        obj_id = json.loads(created)["objective_id"]
        result = await _call(mcp, "conductor_create_task", objective_id=obj_id, title="Task")
        data = json.loads(result)
        assert data["title"] == "Task"

    async def test_approvals_list(self, mcp_state):
        mcp, storage = mcp_state
        result = await _call(mcp, "conductor_list_approvals")
        data = json.loads(result)
        assert data["count"] == 0

    async def test_approve_reject(self, mcp_state):
        mcp, storage = mcp_state
        created = await _call(mcp, "conductor_create_objective", title="Approver")
        obj_id = json.loads(created)["objective_id"]
        runs = storage.list_runs(obj_id)
        approval = storage.create_approval(obj_id, runs[0]["id"], "merge_main")

        # Approve
        result = await _call(mcp, "conductor_approve", approval_id=approval["id"])
        data = json.loads(result)
        assert data["status"] == "approved"

        # Create another and reject
        approval2 = storage.create_approval(obj_id, runs[0]["id"], "delete_data")
        result = await _call(mcp, "conductor_reject", approval_id=approval2["id"])
        data = json.loads(result)
        assert data["status"] == "rejected"

    async def test_pause_resume(self, mcp_state):
        mcp, storage = mcp_state
        created = await _call(mcp, "conductor_create_objective", title="Pausable")
        obj_id = json.loads(created)["objective_id"]
        await _call(mcp, "conductor_pause_objective", objective_id=obj_id)
        obj = storage.get_objective(obj_id)
        assert obj["status"] == "paused"
        await _call(mcp, "conductor_resume_objective", objective_id=obj_id)
        obj = storage.get_objective(obj_id)
        assert obj["status"] == "active"

    async def test_cancel(self, mcp_state):
        mcp, storage = mcp_state
        created = await _call(mcp, "conductor_create_objective", title="Cancellable")
        obj_id = json.loads(created)["objective_id"]
        await _call(mcp, "conductor_cancel_objective", objective_id=obj_id)
        obj = storage.get_objective(obj_id)
        assert obj["status"] == "cancelled"

    async def test_steer(self, mcp_state):
        mcp, storage = mcp_state
        created = await _call(mcp, "conductor_create_objective", title="Steerable")
        obj_id = json.loads(created)["objective_id"]
        result = await _call(mcp, "conductor_steer_objective", objective_id=obj_id, guidance="Focus auth")
        data = json.loads(result)
        assert data["steering_set"] is True
        obj = storage.get_objective(obj_id)
        assert obj["metadata"]["steering"] == "Focus auth"

    async def test_get_status(self, mcp_state):
        mcp, storage = mcp_state
        await _call(mcp, "conductor_create_objective", title="Status Check")
        result = await _call(mcp, "conductor_get_status")
        data = json.loads(result)
        assert "objective" in data
        assert "circuit_breakers" in data

    async def test_dry_run(self, mcp_state):
        mcp, storage = mcp_state
        created = await _call(mcp, "conductor_create_objective", title="Dry Run")
        obj_id = json.loads(created)["objective_id"]
        result = await _call(mcp, "conductor_dry_run", objective_id=obj_id)
        data = json.loads(result)
        assert "would_dispatch" in data