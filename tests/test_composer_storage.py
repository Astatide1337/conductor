"""Tests for Composer storage — specs, plans, plan_tasks, decisions, reports."""

import os
import tempfile

import pytest

from conductor.composer.models import (
    ComposerPlan,
    IntegrationNode,
    NormalizedSpec,
    SpecRepository,
    TaskNode,
    VerificationCommand,
    VerificationSpec,
)
from conductor.composer.storage import ComposerStorage
from conductor.storage import ConductorStorage


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_composer.db")


@pytest.fixture
def cstorage(db_path):
    s = ComposerStorage(db_path)
    s.initialize()
    return s


@pytest.fixture
def conductor_storage(db_path):
    s = ConductorStorage(db_path)
    s.initialize()
    return s


@pytest.fixture
def objective_id(conductor_storage):
    obj = conductor_storage.create_objective(title="Test Objective")
    return obj["id"]


class TestComposerStorageInit:
    def test_initialize_creates_tables(self, db_path):
        s = ComposerStorage(db_path)
        s.initialize()
        assert s._initialized is True
        assert os.path.exists(db_path)

    def test_initialize_is_idempotent(self, db_path):
        s = ComposerStorage(db_path)
        s.initialize()
        s.initialize()
        assert s._initialized is True


class TestSpecStorage:
    def test_create_spec(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Build feature X", "raw spec text")
        assert spec["id"].startswith("spec_")
        assert spec["objective_id"] == objective_id
        assert spec["title"] == "Build feature X"
        assert spec["raw_spec"] == "raw spec text"
        assert spec["status"] == "received"
        assert spec["normalized_spec"] == {}

    def test_get_spec(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        fetched = cstorage.get_spec(spec["id"])
        assert fetched is not None
        assert fetched["id"] == spec["id"]

    def test_get_spec_not_found(self, cstorage):
        assert cstorage.get_spec("nonexistent") is None

    def test_get_spec_by_objective(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        fetched = cstorage.get_spec_by_objective(objective_id)
        assert fetched is not None
        assert fetched["id"] == spec["id"]

    def test_update_spec(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        updated = cstorage.update_spec(
            spec["id"],
            normalized_spec={"goal": "build"},
            status="normalized",
            title="Updated",
        )
        assert updated["status"] == "normalized"
        assert updated["title"] == "Updated"
        assert updated["normalized_spec"]["goal"] == "build"

    def test_raw_spec_persisted(self, cstorage, objective_id):
        raw = "This is the full specification text.\nMultiple lines.\n\nEnd."
        spec = cstorage.create_spec(objective_id, "Test", raw)
        fetched = cstorage.get_spec(spec["id"])
        assert fetched["raw_spec"] == raw


class TestPlanStorage:
    def test_create_plan(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1",
            objective_id=objective_id,
            spec_id=spec["id"],
            status="draft",
            tasks=[
                TaskNode(node_id="task_a", title="A", goal="do A"),
                TaskNode(node_id="task_b", title="B", goal="do B"),
            ],
            integration=IntegrationNode(dependencies=["task_a", "task_b"]),
        )
        result = cstorage.create_plan(objective_id, spec["id"], plan)
        assert result["id"] == "plan_1"
        assert result["status"] == "draft"
        tasks = result["plan_tasks"]
        assert len(tasks) == 3  # 2 implementation + 1 integration
        node_keys = {t["node_key"] for t in tasks}
        assert "task_a" in node_keys
        assert "task_b" in node_keys
        assert "integration" in node_keys

    def test_get_plan(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(id="plan_1", objective_id=objective_id, spec_id=spec["id"])
        cstorage.create_plan(objective_id, spec["id"], plan)
        fetched = cstorage.get_plan("plan_1")
        assert fetched is not None
        assert fetched["id"] == "plan_1"

    def test_get_plan_not_found(self, cstorage):
        assert cstorage.get_plan("nonexistent") is None

    def test_get_plan_by_objective(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(id="plan_1", objective_id=objective_id, spec_id=spec["id"])
        cstorage.create_plan(objective_id, spec["id"], plan)
        fetched = cstorage.get_plan_by_objective(objective_id)
        assert fetched is not None
        assert fetched["id"] == "plan_1"

    def test_update_plan(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(id="plan_1", objective_id=objective_id, spec_id=spec["id"])
        cstorage.create_plan(objective_id, spec["id"], plan)
        updated = cstorage.update_plan("plan_1", status="active", activated_at="2024-01-01T00:00:00Z")
        assert updated["status"] == "active"
        assert updated["activated_at"] == "2024-01-01T00:00:00Z"


class TestPlanTaskStorage:
    def test_list_plan_tasks(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[TaskNode(node_id="task_a"), TaskNode(node_id="task_b")],
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        tasks = cstorage.list_plan_tasks("plan_1")
        assert len(tasks) == 2

    def test_get_plan_task_by_node(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[TaskNode(node_id="task_a")],
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        pt = cstorage.get_plan_task_by_node("plan_1", "task_a")
        assert pt is not None
        assert pt["node_key"] == "task_a"

    def test_update_plan_task(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[TaskNode(node_id="task_a")],
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        pt = cstorage.get_plan_task_by_node("plan_1", "task_a")
        updated = cstorage.update_plan_task(
            pt["id"],
            status="running",
            agents_gateway_task_id="gw_task_1",
            branch="feature/a",
            commit_sha="abc123",
        )
        assert updated["status"] == "running"
        assert updated["agents_gateway_task_id"] == "gw_task_1"
        assert updated["branch"] == "feature/a"
        assert updated["commit_sha"] == "abc123"

    def test_plan_task_dependencies_persisted(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[
                TaskNode(node_id="task_a", dependencies=["task_b"]),
                TaskNode(node_id="task_b"),
            ],
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        pt = cstorage.get_plan_task_by_node("plan_1", "task_a")
        assert pt["dependencies"] == ["task_b"]

    def test_plan_task_file_scope_persisted(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[TaskNode(node_id="task_a", file_scope=["src/", "tests/"])],
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        pt = cstorage.get_plan_task_by_node("plan_1", "task_a")
        assert pt["file_scope"] == ["src/", "tests/"]

    def test_plan_task_verification_persisted(self, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        vs = VerificationSpec(
            required=True,
            commands=[VerificationCommand(name="tests", command="uv run pytest -q", required=True)],
        )
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[TaskNode(node_id="task_a", verification=vs)],
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        pt = cstorage.get_plan_task_by_node("plan_1", "task_a")
        assert pt["verification"]["required"] is True
        assert pt["verification"]["commands"][0]["command"] == "uv run pytest -q"


class TestInteractionDecisionStorage:
    def test_create_decision(self, cstorage, objective_id):
        d = cstorage.create_interaction_decision(
            objective_id=objective_id,
            action="reply",
            reply="Do this thing",
            decision_summary="Spec already defines behavior",
        )
        assert d["id"].startswith("idec_")
        assert d["action"] == "reply"
        assert d["reply"] == "Do this thing"

    def test_list_decisions(self, cstorage, objective_id):
        cstorage.create_interaction_decision(objective_id, "reply", "A", "sum A")
        cstorage.create_interaction_decision(objective_id, "reply", "B", "sum B")
        decisions = cstorage.list_interaction_decisions(objective_id)
        assert len(decisions) == 2

    def test_list_decisions_empty(self, cstorage, objective_id):
        decisions = cstorage.list_interaction_decisions(objective_id)
        assert decisions == []


class TestReportStorage:
    def test_create_report(self, cstorage, objective_id):
        r = cstorage.create_report(
            objective_id=objective_id,
            status="completed",
            html_artifact_ref="/reports/review-report.html",
            json_artifact_ref="/reports/result.json",
            final_branch="composer/obj-1-integration",
            final_commit_sha="abc123",
        )
        assert r["id"].startswith("report_")
        assert r["status"] == "completed"
        assert r["final_branch"] == "composer/obj-1-integration"

    def test_get_report(self, cstorage, objective_id):
        r = cstorage.create_report(objective_id, "completed")
        fetched = cstorage.get_report(r["id"])
        assert fetched is not None
        assert fetched["id"] == r["id"]

    def test_get_report_by_objective(self, cstorage, objective_id):
        cstorage.create_report(objective_id, "completed")
        fetched = cstorage.get_report_by_objective(objective_id)
        assert fetched is not None

    def test_get_report_not_found(self, cstorage):
        assert cstorage.get_report("nonexistent") is None


class TestRestartDurability:
    def test_state_survives_reopen(self, db_path, objective_id, conductor_storage):
        s1 = ComposerStorage(db_path)
        s1.initialize()
        spec = s1.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[TaskNode(node_id="task_a")],
        )
        s1.create_plan(objective_id, spec["id"], plan)

        s2 = ComposerStorage(db_path)
        s2.initialize()
        spec2 = s2.get_spec(spec["id"])
        assert spec2 is not None
        assert spec2["title"] == "Test"
        plan2 = s2.get_plan("plan_1")
        assert plan2 is not None
        tasks2 = s2.list_plan_tasks("plan_1")
        assert len(tasks2) == 1
