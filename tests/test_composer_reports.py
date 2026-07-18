"""Tests for Composer report generator — HTML and JSON reports."""

import json
import os

import pytest

from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.composer.models import (
    IntegrationNode,
    TaskNode,
    VerificationCommand,
    VerificationSpec,
)
from conductor.composer.reports import ReportGenerator
from conductor.composer.storage import ComposerStorage
from conductor.storage import ConductorStorage


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


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
    return conductor_storage.create_objective(title="Test")["id"]


@pytest.fixture
def report_gen(cstorage, tmp_path):
    return ReportGenerator(cstorage, report_dir=str(tmp_path / "reports"))


@pytest.fixture
def plan_with_tasks(cstorage, objective_id):
    from conductor.composer.models import ComposerPlan
    spec = cstorage.create_spec(objective_id, "Test", "raw")
    plan = ComposerPlan(
        id="plan_1", objective_id=objective_id, spec_id=spec["id"],
        tasks=[
            TaskNode(
                node_id="task_a", title="Task A",
                harness_profile="opencode-deepseek",
                status="completed",
                branch="composer/branch-a", commit_sha="sha_a_bandc1234",
            ),
            TaskNode(
                node_id="task_b", title="Task B",
                harness_profile="opencode-deepseek",
                status="completed",
                branch="composer/branch-b", commit_sha="sha_b_3f2e9876",
            ),
        ],
        integration=IntegrationNode(
            required=True, dependencies=["task_a", "task_b"],
            verification=VerificationSpec(
                commands=[VerificationCommand(name="suite", command="uv run pytest", required=True)],
            ),
            status="completed", branch="composer/obj-1-integration", commit_sha="final_sha_abc123",
        ),
    )
    cstorage.create_plan(objective_id, spec["id"], plan)
    return plan


class TestReportGeneration:
    def test_generate_report(self, report_gen, cstorage, objective_id, plan_with_tasks):
        plan_dict = cstorage.get_plan_by_objective(objective_id)
        spec = cstorage.get_spec_by_objective(objective_id)
        result = report_gen.generate_report(
            objective_id=objective_id,
            spec=spec,
            plan=plan_dict,
            final_status="completed",
            final_branch="composer/obj-1-integration",
            final_commit_sha="final_sha_abc123",
        )
        assert result["id"].startswith("report_")
        assert result["status"] == "completed"
        assert result["final_branch"] == "composer/obj-1-integration"

    def test_html_report_generated(self, report_gen, cstorage, objective_id, plan_with_tasks):
        plan_dict = cstorage.get_plan_by_objective(objective_id)
        spec = cstorage.get_spec_by_objective(objective_id)
        result = report_gen.generate_report(
            objective_id=objective_id, spec=spec, plan=plan_dict, final_status="completed",
            final_branch="composer/obj-1-integration", final_commit_sha="final_sha",
        )
        assert os.path.exists(result["html_artifact_ref"])
        html = open(result["html_artifact_ref"]).read()
        assert "<html" in html
        assert "Composer Review Report" in html
        assert "task_a" in html
        assert "task_b" in html

    def test_json_report_generated(self, report_gen, cstorage, objective_id, plan_with_tasks):
        plan_dict = cstorage.get_plan_by_objective(objective_id)
        spec = cstorage.get_spec_by_objective(objective_id)
        result = report_gen.generate_report(
            objective_id=objective_id, spec=spec, plan=plan_dict, final_status="completed",
            final_branch="composer/obj-1-integration", final_commit_sha="final_sha",
        )
        assert os.path.exists(result["json_artifact_ref"])
        json_data = json.loads(open(result["json_artifact_ref"]).read())
        assert json_data["objective_id"] == objective_id
        assert json_data["final_branch"] == "composer/obj-1-integration"
        assert "task_graph" in json_data

    def test_report_includes_task_graph(self, report_gen, cstorage, objective_id, plan_with_tasks):
        plan_dict = cstorage.get_plan_by_objective(objective_id)
        spec = cstorage.get_spec_by_objective(objective_id)
        result = report_gen.generate_report(
            objective_id=objective_id, spec=spec, plan=plan_dict, final_status="completed",
            final_branch="composer/obj-1-integration", final_commit_sha="final_sha",
        )
        html = open(result["html_artifact_ref"]).read()
        assert "Task Graph" in html
        assert "opencode-deepseek" in html

    def test_report_includes_verification_matrix(self, report_gen, cstorage, objective_id, plan_with_tasks):
        plan_dict = cstorage.get_plan_by_objective(objective_id)
        spec = cstorage.get_spec_by_objective(objective_id)
        result = report_gen.generate_report(
            objective_id=objective_id, spec=spec, plan=plan_dict, final_status="completed",
            final_branch="composer/obj-1-integration", final_commit_sha="final_sha",
            verification_results=[{"name": "test suite", "status": "passed", "passed": True}],
        )
        html = open(result["html_artifact_ref"]).read()
        assert "Verification Matrix" in html
        assert "test suite" in html

    def test_report_includes_branches_and_commits(self, report_gen, cstorage, objective_id, plan_with_tasks):
        plan_dict = cstorage.get_plan_by_objective(objective_id)
        spec = cstorage.get_spec_by_objective(objective_id)
        result = report_gen.generate_report(
            objective_id=objective_id, spec=spec, plan=plan_dict, final_status="completed",
            final_branch="composer/obj-1-integration", final_commit_sha="final_sha",
        )
        html = open(result["html_artifact_ref"]).read()
        assert "composer/branch-a" in html
        assert "composer/obj-1-integration" in html

    def test_report_does_not_expose_secrets(self, report_gen, cstorage, objective_id, plan_with_tasks):
        plan_dict = cstorage.get_plan_by_objective(objective_id)
        spec_dict = {
            "title": "Test spec",
            "raw_spec": "secret_api_key=sk-or-v1-XXXXXXXXXXXXXXXXXXXXXXXX",
            "normalized_spec": {},
        }
        result = report_gen.generate_report(
            objective_id=objective_id, spec=spec_dict, plan=plan_dict, final_status="completed",
            final_branch="composer/obj-1-integration", final_commit_sha="final_sha",
        )
        json_data = json.loads(open(result["json_artifact_ref"]).read())
        html = open(result["html_artifact_ref"]).read()
        assert "sk-or-v1" not in html
        assert "sk-or-v1" not in json.dumps(json_data)

    def test_report_includes_interactions(self, report_gen, cstorage, objective_id, plan_with_tasks):
        # Add an interaction decision
        cstorage.create_interaction_decision(
            objective_id,
            action="reply",
            reply="Follow spec",
            decision_summary="Spec defines behavior",
        )

        plan_dict = cstorage.get_plan_by_objective(objective_id)
        spec = cstorage.get_spec_by_objective(objective_id)
        result = report_gen.generate_report(
            objective_id=objective_id, spec=spec, plan=plan_dict, final_status="completed",
            final_branch="composer/obj-1-integration", final_commit_sha="final_sha",
        )
        html = open(result["html_artifact_ref"]).read()
        assert "Interactions" in html
        assert "Follow spec" in html

    def test_report_stored_in_storage(self, report_gen, cstorage, objective_id, plan_with_tasks):
        plan_dict = cstorage.get_plan_by_objective(objective_id)
        spec = cstorage.get_spec_by_objective(objective_id)
        report_gen.generate_report(
            objective_id=objective_id, spec=spec, plan=plan_dict, final_status="completed",
            final_branch="composer/obj-1-integration", final_commit_sha="final_sha",
        )
        stored = cstorage.get_report_by_objective(objective_id)
        assert stored is not None
        assert stored["status"] == "completed"
