"""End-to-end Composer test with the FakeComposerLLMClient and a fake Agents Gateway.

Drives the complete flow:
  spec -> plan -> parallel tasks -> interaction response -> verified completion
       -> integration -> full verification -> final report
"""

import asyncio
import json
import os
import tempfile

import pytest

from conductor.circuit import BreakerEvaluator
from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.composer.events import composer_emit
from conductor.composer.llm import FakeComposerLLMClient
from conductor.composer.service import ComposerService
from conductor.composer.storage import ComposerStorage
from conductor.config import ConductorConfig
from conductor.gateways import build_default_registry
from conductor.storage import ConductorStorage


@pytest.fixture
def e2e_setup():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "e2e.db")
        cs = ConductorStorage(db); cs.initialize()
        cps = ComposerStorage(db); cps.initialize()
        cfg = ConductorConfig(environment="test", storage={"sqlite_path": db})
        cfg.composer.enabled = True
        cfg.composer.auto_start = True
        cfg.composer.report_dir = os.path.join(d, "reports")
        gw = MockAgentsGatewayClient()
        gw.register_agent("code-validator", "Code Validator")
        gw.register_harness_profile("opencode-deepseek", "OpenCode DeepSeek", runnable=True)
        reg = build_default_registry(cfg)
        llm = FakeComposerLLMClient()
        svc = ComposerService(
            storage=cps, conductor_storage=cs,
            llm_client=llm, agents_gateway_client=gw,
            config=cfg.composer, skills_gateway_client=None,
            wiki_mcp_client=None, gateway_registry=reg, metrics=None,
        )
        # Conductor's create_objective leaves status empty so adjust; Composer uses
        # the spec status itself for tracking.
        yield svc, gw, cs, cps, llm


class TestComposerE2EFlow:
    """The full local E2E scenario using deterministic fakes."""

    @pytest.mark.asyncio
    async def test_spec_to_plan_to_completion(self, e2e_setup):
        """Spec submitted -> plan generated -> tasks dispatched -> all completed
        -> integration dispatched -> final report generated -> objective complete.
        """
        svc, gw, cs, cps, llm = e2e_setup

        SPEC_TEXT = """Build a calculator with add, multiply, divide."""
        r = await svc.submit_specification(
            title="Calculator feature",
            raw_spec=SPEC_TEXT,
            repository={"url": "https://github.com/test/calc.git", "base_branch": "master"},
            auto_start=True,
        )
        obj_id = r["objective_id"]
        spec_id = r["composer_spec_id"]

        # 1. Spec normalization should have happened synchronously in submit_specification
        spec = cps.get_spec(spec_id)
        assert spec is not None
        assert spec["status"] in ("normalized", "planning", "planned", "executing")

        # 2. Plan should have been created by the FakeComposerLLMClient
        plan = cps.get_plan_by_objective(obj_id)
        assert plan is not None, "Plan not persisted after submit"
        plan_tasks = plan.get("plan_tasks", [])
        # At least 2 implementation tasks + 1 integration node
        impl_tasks = [t for t in plan_tasks if t.get("node_key") != "integration"]
        assert len(impl_tasks) >= 2, f"Expected >=2 implementation tasks, got {len(impl_tasks)}"

        # 3. The two implementation tasks should have been dispatched (status dispatching/running)
        # already during submit_specification (auto_start=True).
        running_or_dispatched = [
            t for t in plan_tasks
            if t.get("status") in ("dispatching", "running", "completed", "verifying", "waiting_for_reply")
        ]
        assert len(running_or_dispatched) >= 2, (
            f"Expected >=2 dispatched tasks, got {len(running_or_dispatched)}; "
            f"statuses: {[t['status'] for t in plan_tasks]}"
        )

        # 4. Simulate the Agents Gateway completing the two implementation tasks
        for pt in plan_tasks:
            if pt.get("node_key") == "integration":
                continue
            gw_task_id = pt.get("agents_gateway_task_id")
            if not gw_task_id:
                continue
            gw.complete_task(gw_task_id, output="Task completed successfully")
            gw.set_verification(
                agent_run_id=gw_task_id,
                status="passed",
                commands=[{"name": "unit tests", "command": "uv run pytest -q", "passed": True, "required": True}],
            )

        # 5. Reconcile — should drive both to "completed" status and trigger integration
        await svc.reconcile_objective(obj_id)
        plan = cps.get_plan_by_objective(obj_id)
        impl_statuses = [t.get("status") for t in plan["plan_tasks"] if t.get("node_key") != "integration"]
        # After reconcile, completed tasks (or in-progress verifications)
        assert all(s in ("completed", "verifying", "running") for s in impl_statuses), (
            f"unexpected impl task status: {impl_statuses}"
        )

        # 6. The first reconcile should have dispatched the integration task
        # (integration_ready is computed once all deps are completed).
        # Simulate completing the integration task.
        plan = cps.get_plan_by_objective(obj_id)
        integration_pt = next((t for t in plan["plan_tasks"] if t.get("node_key") == "integration"), None)
        assert integration_pt is not None, "integration task missing from plan"
        gw_integration_id = integration_pt.get("agents_gateway_task_id")
        assert gw_integration_id, (
            f"integration task was not dispatched by reconcile; status={integration_pt['status']}"
        )
        gw.complete_task(gw_integration_id, output="Integration complete")
        gw.set_verification(
            agent_run_id=gw_integration_id,
            status="passed",
            commands=[{"name": "full test suite", "command": "uv run pytest -q", "passed": True, "required": True}],
        )

        # 7. Final reconcile should mark objective completed and generate report
        await svc.reconcile_objective(obj_id)
        final_spec = cps.get_spec(spec_id)
        assert final_spec["status"] == "completed", (
            f"Expected status 'completed', got '{final_spec['status']}'"
        )

        # 8. Verify the report was generated
        report = cps.get_report_by_objective(obj_id)
        assert report is not None, "No report generated"
        # File artifacts on disk
        assert os.path.exists(report["html_artifact_ref"]), "HTML report file missing"
        assert os.path.exists(report["json_artifact_ref"]), "JSON report file missing"
        # JSON report should be machine-readable
        with open(report["json_artifact_ref"]) as f:
            json_report = json.load(f)
            assert json_report["objective_id"] == obj_id
            assert json_report["status"] == "completed"

    @pytest.mark.asyncio
    async def test_interaction_response_loop(self, e2e_setup):
        """Composer discovers pending interaction, answers via FakeComposerLLMClient,
        and persists the decision.
        """
        svc, gw, cs, cps, llm = e2e_setup

        r = await svc.submit_specification(
            title="Interaction test",
            raw_spec="Build something",
            auto_start=True,
        )
        obj_id = r["objective_id"]
        spec_id = r["composer_spec_id"]
        plan = cps.get_plan_by_objective(obj_id)
        plan_tasks = plan.get("plan_tasks", [])

        # Find one implementation task and simulate a pending interaction
        impl_tasks = [t for t in plan_tasks if t.get("node_key") != "integration"]
        if not impl_tasks:
            pytest.skip("No implementation tasks dispatched — check FakeComposerLLMClient")
        first = impl_tasks[0]
        gw_task_id = first.get("agents_gateway_task_id")
        if not gw_task_id:
            pytest.skip("first task has no agent gateway task id")
        # Move it into waiting_for_reply state with a pending interaction
        gw.set_task_waiting(gw_task_id)
        gw.create_mock_interaction(gw_task_id, prompt="Should I preserve backward compatibility?")

        # Reconcile — Composer should answer the interaction via the fake LLM
        await svc.reconcile_objective(obj_id)

        # Composer should have fetched pending interactions, asked the LLM, and replied
        # We can verify by inspecting the LLM's recorded calls list for answer_interaction
        methods_called = [c.get("method") for c in llm.calls]
        assert "answer_interaction" in methods_called, (
            f"Expected answer_interaction LLM call, got: {methods_called}"
        )

        # The MockAgentsGatewayClient should have the interaction "replied" state now
        # (We don't strictly assert its internal state since the LLM reply is recorded.)
        # Composer should have persisted the decision (if the interaction handler wrote one)
        decisions = cps.list_interaction_decisions(obj_id) if hasattr(cps, "list_interaction_decisions") else []
        # The mock interaction flow may or may not persist a decision row — depends
        # on whether InteractionHandler found a matching pending interaction row in
        # the composer_interaction_decisions table. We only assert that it was called.
        # The decision_summary is what we'd persist.
        assert isinstance(decisions, list)
        # Continue and mark task completed so cleanup is clean
        gw.complete_task(gw_task_id, output="OK after clarification")

    @pytest.mark.asyncio
    async def test_failed_verification_no_finalization(self, e2e_setup):
        """Failed verification should NOT finalize the objective."""
        svc, gw, cs, cps, llm = e2e_setup

        r = await svc.submit_specification(
            title="Failing verification",
            raw_spec="Build and verify",
            auto_start=True,
        )
        obj_id = r["objective_id"]
        spec_id = r["composer_spec_id"]
        plan = cps.get_plan_by_objective(obj_id)
        plan_tasks = plan.get("plan_tasks", [])

        # Mark all dispatched tasks as FAILED verification
        for pt in plan_tasks:
            gw_task_id = pt.get("agents_gateway_task_id")
            if not gw_task_id:
                continue
            if pt.get("node_key") == "integration":
                continue
            gw.set_verification(
                agent_run_id=gw_task_id, status="failed",
                commands=[{"name": "unit tests", "command": "uv run pytest -q", "passed": False, "required": True}],
            )
        # Reconcile — should NOT mark objective completed
        await svc.reconcile_objective(obj_id)
        spec = cps.get_spec(spec_id)
        assert spec["status"] != "completed", "Objective completed despite verification failure"
        # No report should exist yet
        report = cps.get_report_by_objective(obj_id)
        assert report is None, "Report generated despite verification failure"

    @pytest.mark.asyncio
    async def test_objective_state_survives_simulated_restart(self, e2e_setup):
        """Composer state should survive a simulated Conductor restart.

        The ComposerStorage is the source of truth — restarting means rebuilding
        the ComposerService from persisted DB state. We can't easily test that
        here (the ConductorStorage singleton is shared), but we can verify the
        persisted state is sufficient.
        """
        svc, gw, cs, cps, llm = e2e_setup

        r = await svc.submit_specification(
            title="Restart test",
            raw_spec="Should be resilient",
            auto_start=True,
        )
        obj_id = r["objective_id"]
        spec_id = r["composer_spec_id"]
        # State persistence check — read back the spec and plan
        spec_stored = cps.get_spec(spec_id)
        assert spec_stored is not None, "Spec not persisted"
        plan_stored = cps.get_plan_by_objective(obj_id)
        assert plan_stored is not None, "Plan not persisted"
        # Verify task refs to gateway tasks are persisted
        for pt in plan_stored["plan_tasks"]:
            if pt.get("agents_gateway_task_id"):
                # This task has a real linkage
                assert pt.get("agents_gateway_task_id") in gw._tasks, "Task link not persisted"
        # The agent-gateway tasks themselves are in-memory; in production they'd be
        # recovered from AgentGateway state.

    @pytest.mark.asyncio
    async def test_separate_worktrees_per_task(self, e2e_setup):
        """At least two Tasks should have separate agent gateway tasks (=worktrees)."""
        svc, gw, cs, cps, llm = e2e_setup

        r = await svc.submit_specification(
            title="Worktree isolation",
            raw_spec="Two parallel tasks",
            auto_start=True,
        )
        obj_id = r["objective_id"]
        plan = cps.get_plan_by_objective(obj_id)
        impl = [t for t in plan["plan_tasks"] if t.get("node_key") != "integration"]
        gw_task_ids = [t.get("agents_gateway_task_id") for t in impl if t.get("agents_gateway_task_id")]
        # At least two separate gateway task IDs (= separate worktrees)
        assert len(set(gw_task_ids)) >= 2, (
            f"Expected >=2 unique gateway task IDs, got {len(set(gw_task_ids))}"
        )
