"""Capability validation tests — required_capabilities on a task block dispatch."""

import json
import os
import tempfile

import pytest

from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.clients.skills_gateway import MockSkillsGatewayClient
from conductor.config import ConductorConfig
from conductor.dispatch import dispatch_task
from conductor.gateways import build_default_registry
from conductor.gateways.models import GatewayConfig
from conductor.gateways.registry import GatewayRegistry
from conductor.gateways.validation import (
    validate_required_capabilities,
    get_required_capabilities_from_task,
)
from conductor.storage import ConductorStorage


@pytest.fixture
def fresh_state():
    with tempfile.TemporaryDirectory() as d:
        s = ConductorStorage(os.path.join(d, "test.db"))
        s.initialize()
        gw = MockAgentsGatewayClient()
        gw.register_agent("code-validator", "Code Validator")
        sk = MockSkillsGatewayClient()
        sk.register("pytest-mcp", "Pytest MCP")
        cfg = ConductorConfig(environment="test")
        reg = build_default_registry(cfg)
        yield s, gw, sk, reg


def _task(state, *, required_capabilities=None, required_skills=None,
           agent="code-validator"):
    s, gw, sk, reg = state
    obj = s.create_objective(title="Cap Test", description="probe")
    run = s.create_run(obj["id"], planner_mode="manual")
    md = {}
    if required_capabilities:
        md["required_capabilities"] = required_capabilities
    task = s.create_task(
        objective_id=obj["id"], run_id=run["id"], title="Probe",
        task_type="ship", required_skills=required_skills or [],
        metadata=md,
    )
    return task


class TestValidateRequiredCapabilities:
    def test_empty_capability_list_valid(self):
        reg = build_default_registry(ConductorConfig(environment="test"))
        r = validate_required_capabilities(reg, [])
        assert r.valid is True
        assert r.missing == []

    def test_known_capability_satisfied(self):
        reg = build_default_registry(ConductorConfig(environment="test"))
        r = validate_required_capabilities(reg, ["execution.task.create"])
        assert r.valid is True
        assert "execution.task.create" in r.satisfied

    def test_unknown_capability_missing(self):
        reg = build_default_registry(ConductorConfig(environment="test"))
        r = validate_required_capabilities(reg, ["never.exists"])
        assert r.valid is False
        assert "never.exists" in r.missing

    def test_mixed(self):
        reg = build_default_registry(ConductorConfig(environment="test"))
        r = validate_required_capabilities(reg, [
            "execution.task.create", "never.exists",
        ])
        assert r.valid is False
        assert "execution.task.create" in r.satisfied
        assert "never.exists" in r.missing


class TestGetRequiredCapabilitiesFromTask:
    def test_empty_when_no_metadata(self):
        assert get_required_capabilities_from_task({"metadata": {}}) == []

    def test_returns_list_when_set(self):
        t = {"metadata": {"required_capabilities": ["a", "b"]}}
        assert get_required_capabilities_from_task(t) == ["a", "b"]

    def test_handles_partial_dict(self):
        assert get_required_capabilities_from_task({}) == []
        assert get_required_capabilities_from_task(None) == []


class TestDispatchBlocksMissingCapability:
    def test_dispatch_blocked_on_missing_capability(self, fresh_state):
        s, gw, sk, reg = fresh_state
        task = _task(fresh_state, required_capabilities=["never.exists"])
        result = dispatch_task(
            s, gw, task["id"],
            skills_client=sk, registry=reg,
        )
        assert result["agent_run"] is None
        assert "missing required capabilities" in result["error"]
        assert "never.exists" in result["missing_capabilities"]
        # No agent_run row created
        assert s.list_inflight_agent_runs() == []
        # Temporal event emitted
        from conductor.events import list_events
        evts = list_events(s, objective_id=task["objective_id"])
        assert any(e.event_type == "task.capabilities_validation_failed" for e in evts)

    def test_dispatch_allowed_when_capability_satisfied(self, fresh_state):
        s, gw, sk, reg = fresh_state
        task = _task(fresh_state, required_capabilities=["execution.task.create"])
        result = dispatch_task(
            s, gw, task["id"],
            skills_client=sk, registry=reg,
        )
        # On success, dispatch_task returns the agent_run dict directly (no `agent_run` key)
        assert "agents_gateway_task_id" in result
        assert result["status"] in ("running", "dispatched")
        # An agent_run row was created
        assert len(s.list_inflight_agent_runs()) >= 1

    def test_dispatch_with_satisfied_capability_emits_validated_event(self, fresh_state):
        from conductor.events import list_events
        s, gw, sk, reg = fresh_state
        task = _task(fresh_state, required_capabilities=["execution.task.create", "skills.list"])
        dispatch_task(s, gw, task["id"], skills_client=sk, registry=reg)
        evts = list_events(s, objective_id=task["objective_id"])
        # We should see both task.capabilities_validated AND task.skills_validated
        types = [e.event_type for e in evts]
        assert "task.capabilities_validated" in types
        assert "task.skills_validated" in types

    def test_dispatch_no_registry_skips_capability_gate(self, fresh_state):
        """Without a registry, no capability validation runs — task goes through."""
        s, gw, sk, reg = fresh_state
        task = _task(fresh_state, required_capabilities=["never.exists"])
        result = dispatch_task(s, gw, task["id"], skills_client=sk, registry=None)
        # Even though "never.exists" is required, no registry means no gate
        assert s.list_inflight_agent_runs() != []
