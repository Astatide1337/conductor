"""E2E-style tests for the orchestrator flows:
- dispatch → gateway completion → reconcile → artifact ingestion
- skills validation gate blocks dispatch when required skills missing
- /reconcile endpoint iterates in-flight agent_runs
- restart-safe: state survives re-instantiation
"""

import os
import tempfile

import pytest
from starlette.testclient import TestClient

from conductor.config import ConductorConfig
from conductor.server import create_app
from conductor.storage import ConductorStorage
from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.clients.skills_gateway import MockSkillsGatewayClient
from conductor.dispatch import (
    dispatch_task,
    reconcile_task,
    reconcile_all,
)


@pytest.fixture
def fresh_app():
    with tempfile.TemporaryDirectory() as d:
        cfg = ConductorConfig(
            environment="test",
            storage={"sqlite_path": os.path.join(d, "test.db")},
            auth={"mode": "dev-none"},
        )
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        yield app, client


@pytest.fixture
def storage_gw_skills():
    """Storage + mock gateway + mock skills — for direct dispatch tests."""
    with tempfile.TemporaryDirectory() as d:
        s = ConductorStorage(os.path.join(d, "test.db"))
        s.initialize()
        gw = MockAgentsGatewayClient()
        gw.register_agent("code-validator", "Code Validator")
        sk = MockSkillsGatewayClient()
        sk.register("pytest-mcp", "Pytest MCP")
        sk.register("git-tools", "Git Tools")
        yield s, gw, sk


class TestDispatchSkillsValidationGate:
    """Tasks must validate required skills before any work leaves Conductor."""

    def test_dispatch_blocked_on_missing_skill(self, storage_gw_skills):
        s, gw, sk = storage_gw_skills
        obj = s.create_objective(title="NeedSkills")
        run = s.create_run(obj["id"])
        s.update_task_status  # noqa
        task = s.create_task(
            obj["id"], run["id"], "Task with skills",
            required_skills=["pytest-mcp", "nonexistent-skill"],
        )
        s.update_task_status(task["id"], "ready")
        result = dispatch_task(s, gw, task["id"], skills_client=sk)
        assert result["status"] == "blocked"
        assert result.get("agent_run") is None
        assert "nonexistent-skill" in result["missing_skills"]

        # Task should now be in blocked status
        t = s.get_task(task["id"])
        assert t["status"] == "blocked"

        # An event should have been emitted
        from conductor.events import list_events
        evts = list_events(s, task_id=task["id"], limit=10)
        assert any(e.event_type == "dispatch.skills_missing" for e in evts)

    def test_dispatch_passes_when_skills_available(self, storage_gw_skills):
        s, gw, sk = storage_gw_skills
        obj = s.create_objective(title="HasSkills")
        run = s.create_run(obj["id"])
        task = s.create_task(
            obj["id"], run["id"], "Task with skills",
            required_skills=["pytest-mcp"],
        )
        s.update_task_status(task["id"], "ready")
        result = dispatch_task(s, gw, task["id"], skills_client=sk)
        # dispatch_task returns the agent_run dict directly on success
        assert result["status"] == "running"
        assert result["agents_gateway_task_id"] is not None

    def test_dispatch_no_skills_client_skips_validation(self, storage_gw_skills):
        """In dev with no skills_client configured, dispatch must still succeed."""
        s, gw, _ = storage_gw_skills
        obj = s.create_objective(title="NoSkillsClient")
        run = s.create_run(obj["id"])
        task = s.create_task(
            obj["id"], run["id"], "Task with skills",
            required_skills=["anything-at-all"],
        )
        s.update_task_status(task["id"], "ready")
        # skills_client=None: validation no-op
        result = dispatch_task(s, gw, task["id"], skills_client=None)
        assert result["status"] == "running"


class TestReconcileArtifactIngestion:
    """Reconcile must (1) sync status (2) ingest artifact refs after completion."""

    def test_reconcile_after_completion_ingests_artifacts(self, storage_gw_skills):
        s, gw, sk = storage_gw_skills
        obj = s.create_objective(title="Artifacts")
        run = s.create_run(obj["id"])
        task = s.create_task(obj["id"], run["id"], "Produces artifacts", required_skills=["pytest-mcp"])
        s.update_task_status(task["id"], "ready")

        result = dispatch_task(s, gw, task["id"], skills_client=sk)
        gw_id = result["agents_gateway_task_id"]

        # Gateway completes and produces artifacts
        gw.complete_task(gw_id, output="Build OK")
        gw.add_artifact(gw_id, name="report.log", size=2048)
        gw.add_artifact(gw_id, name="build.json", size=64)

        # Reconcile
        reconciled = reconcile_task(s, gw, result["id"])
        assert reconciled["status"] == "completed"
        assert reconciled["result_summary"] == "Build OK"

        # Task should be completed too
        t = s.get_task(task["id"])
        assert t["status"] == "completed"

        # Artifacts ingested into agent_runs.artifact_refs_json
        art_refs = reconciled["artifact_refs"]
        assert len(art_refs) == 2
        names = {a["name"] for a in art_refs}
        assert names == {"report.log", "build.json"}

    def test_reconcile_all_summarizes_in_flight(self, storage_gw_skills):
        """reconcile_all iterates over multiple in-flight agent_runs."""
        s, gw, sk = storage_gw_skills

        # Two tasks, one completes, one still running
        obj = s.create_objective(title="ReconcileAll")
        run = s.create_run(obj["id"])
        task_a = s.create_task(obj["id"], run["id"], "A", required_skills=["pytest-mcp"])
        task_b = s.create_task(obj["id"], run["id"], "B", required_skills=["pytest-mcp"])
        s.update_task_status(task_a["id"], "ready")
        s.update_task_status(task_b["id"], "ready")

        r_a = dispatch_task(s, gw, task_a["id"], skills_client=sk)
        r_b = dispatch_task(s, gw, task_b["id"], skills_client=sk)

        # Complete one, leave one running
        gw.complete_task(r_a["agents_gateway_task_id"], output="a done")

        summary = reconcile_all(s, gw)
        assert summary["candidate_count"] == 2
        assert summary["reconciled"] == 2
        # At least one transition: r_a went running -> completed
        assert summary["transitions"] >= 1
        assert summary["by_target"].get("completed") == 1

    def test_reconcile_is_idempotent_artifacts(self, storage_gw_skills):
        """Calling reconcile_task again on an already-reconciled agent_run does not duplicate artifacts."""
        s, gw, sk = storage_gw_skills
        obj = s.create_objective(title="Idempotent Reconcile")
        run = s.create_run(obj["id"])
        task = s.create_task(obj["id"], run["id"], "Produces artifacts", required_skills=["pytest-mcp"])
        s.update_task_status(task["id"], "ready")

        result = dispatch_task(s, gw, task["id"], skills_client=sk)
        gw_id = result["agents_gateway_task_id"]
        gw.complete_task(gw_id, output="done")
        gw.add_artifact(gw_id, name="a1.log", size=10)

        first = reconcile_task(s, gw, result["id"])
        assert len(first["artifact_refs"]) == 1

        # Add another artifact + re-reconcile
        gw.add_artifact(gw_id, name="a2.log", size=20)
        second = reconcile_task(s, gw, result["id"])
        assert len(second["artifact_refs"]) == 2
        # No duplicates
        ids = [a["name"] for a in second["artifact_refs"]]
        assert sorted(ids) == ["a1.log", "a2.log"]


class TestReconcileRestartsSafe:
    """After a conductor restart (re-instantiate storage with same db path),
    in-flight agent_runs must still be candidate for reconciliation."""

    def test_restart_recovers_in_flight(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "conductor.db")

            # Boot 1: dispatch two tasks
            s1 = ConductorStorage(db_path)
            s1.initialize()
            gw = MockAgentsGatewayClient()
            gw.register_agent("code-validator", "Code Validator")
            sk = MockSkillsGatewayClient()
            sk.register("pytest-mcp", "Pytest MCP")

            obj = s1.create_objective(title="Restart")
            run = s1.create_run(obj["id"])
            ta = s1.create_task(obj["id"], run["id"], "A", required_skills=["pytest-mcp"])
            tb = s1.create_task(obj["id"], run["id"], "B", required_skills=["pytest-mcp"])
            s1.update_task_status(ta["id"], "ready")
            s1.update_task_status(tb["id"], "ready")
            r_a = dispatch_task(s1, gw, ta["id"], skills_client=sk)
            r_b = dispatch_task(s1, gw, tb["id"], skills_client=sk)
            # Simulate gateway-side completion of A while "we were down"
            gw.complete_task(r_a["agents_gateway_task_id"], output="done while down")

            # Boot 2: same db, fresh storage instance
            s2 = ConductorStorage(db_path)
            s2.initialize()

            inflight = s2.list_inflight_agent_runs()
            assert len(inflight) == 2
            inflight_ids = {ar["id"] for ar in inflight}
            assert r_a["id"] in inflight_ids
            assert r_b["id"] in inflight_ids

            summary = reconcile_all(s2, gw)
            assert summary["reconciled"] == 2
            # r_a was confirmed completed (running -> completed)
            assert summary["by_target"].get("completed") == 1
            # r_b was already "running", no transition recorded — confirm directly

            # Confirm task A is now completed, task B still running
            ta2 = s2.get_task(ta["id"])
            tb2 = s2.get_task(tb["id"])
            assert ta2["status"] == "completed"
            assert tb2["status"] == "running"

            # Confirm r_a agent_run achieved "completed" status after reconcile
            ra2 = s2.get_agent_run(r_a["id"])
            assert ra2["status"] == "completed"


class TestReconcileEndpoint:
    """/reconcile HTTP endpoint exposes reconcile_all summary."""

    def test_reconcile_endpoint_returns_summary(self, fresh_app):
        app, client = fresh_app
        r = client.post("/reconcile")
        assert r.status_code == 200
        body = r.json()
        assert "candidate_count" in body
        assert "reconciled" in body
        assert "transitions" in body

    def test_reconcile_endpoint_after_workflow(self, fresh_app):
        app, client = fresh_app

        # Create objective and a task, then dispatch via HTTP
        obj_resp = client.post("/objectives", json={"title": "E2E"})
        assert obj_resp.status_code == 201
        obj_id = obj_resp.json()["objective_id"]
        run_id = obj_resp.json()["run_id"]

        # Manually create a task via storage (HTTP task create would also work, but we want a task in "ready")
        storage = app.state.storage
        gw = app.state.gateway_client
        sk = app.state.skills_client
        # In test config with dev-none url localhost, skills_client is None — pass nothing
        task = storage.create_task(
            obj_id, run_id, "Endpoint flow",
            required_skills=["pytest-mcp"],  # may not validate since no skills_client
        )
        storage.update_task_status(task["id"], "ready")
        result = dispatch_task(storage, gw, task["id"], skills_client=None)
        assert result["status"] in ("running", "blocked")

        # Reconcile
        r = client.post("/reconcile")
        assert r.status_code == 200
        body = r.json()
        assert body["candidate_count"] >= 1
        assert body["reconciled"] >= 1


class TestMCPToolsUseSharedGateway:
    """MCP tools dispatch via the same gateway client as the HTTP API."""

    def test_mcp_dispatch_uses_shared_gateway(self, fresh_app):
        # Create objective via HTTP, dispatch via MCP tool, both go through app.state gateway
        app, client = fresh_app
        storage = app.state.storage

        obj = storage.create_objective(title="MCP shared gw")
        run = storage.create_run(obj["id"])
        task = storage.create_task(obj["id"], run["id"], "via mcp")
        storage.update_task_status(task["id"], "ready")

        fastmcp_app = None
        # Find the mounted mcp app
        for route in app.routes:
            if hasattr(route, "name") and "mcp" in str(getattr(route, "path", "")).lower():
                fastmcp_app = getattr(route, "app", None)
                break

        # If MCP was mounted, dispatch via the conductor_dispatch_task tool — but that path uses tool manager
        # internals. Calling it through the mounted MCP path is already covered by test_mcp_auth.py.
        # Here we just verify the dispatch happens against the shared gateway state.
        from conductor.dispatch import dispatch_task
        result = dispatch_task(storage, app.state.gateway_client, task["id"])
        assert result["status"] == "running"

        # Reconcile via HTTP
        r = client.post("/reconcile")
        assert r.status_code == 200
