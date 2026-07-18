"""Tests for Composer integration task — branches collected and integrated."""

import pytest

from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.composer.integration import IntegrationDispatcher
from conductor.composer.models import (
    ComposerPlan,
    IntegrationNode,
    TaskNode,
    VerificationCommand,
    VerificationSpec,
)
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
def gw():
    client = MockAgentsGatewayClient()
    client.register_harness_profile("opencode-deepseek")
    return client


@pytest.fixture
def dispatcher(cstorage, gw, conductor_storage):
    return IntegrationDispatcher(
        cstorage, gw,
        integration_harness_profile="opencode-deepseek",
        conductor_storage=conductor_storage,
    )


@pytest.fixture
def objective_id(conductor_storage):
    return conductor_storage.create_objective(title="Test")["id"]


class TestDispatchIntegration:
    @pytest.mark.asyncio
    async def test_dispatch_integration(self, dispatcher, gw, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        # Create plan with completed tasks
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[
                TaskNode(
                    node_id="task_a", status="completed",
                    branch="composer/branch-a", commit_sha="sha_a",
                ),
                TaskNode(
                    node_id="task_b", status="completed",
                    branch="composer/branch-b", commit_sha="sha_b",
                ),
            ],
            integration=IntegrationNode(
                required=True, dependencies=["task_a", "task_b"],
                verification=VerificationSpec(
                    commands=[VerificationCommand(name="suite", command="uv run pytest", required=True)],
                ),
            ),
        )
        cstorage.create_plan(objective_id, spec["id"], plan)

        result = dispatcher.dispatch_integration(plan, {"normalized_spec": {}}, objective_id, "")
        assert result is not None
        assert result["node_id"] == "integration"
        assert "gw_task_id" in result

    def test_dispatch_no_integration_node(self, dispatcher, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        from conductor.composer.models import ComposerPlan as CP
        plan = CP(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[TaskNode(node_id="task_a", status="completed")],
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        result = dispatcher.dispatch_integration(plan, {}, objective_id, "")
        assert result is None

    def test_dispatch_integration_harness_not_runnable(self, cstorage, conductor_storage, objective_id):
        gw = MockAgentsGatewayClient()
        gw.register_harness_profile("opencode-deepseek", runnable=False)
        dispatcher = IntegrationDispatcher(cstorage, gw,
                                           conductor_storage=conductor_storage)
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[TaskNode(node_id="task_a", status="completed", branch="b", commit_sha="c")],
            integration=IntegrationNode(required=True, dependencies=["task_a"]),
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        result = dispatcher.dispatch_integration(plan, {"normalized_spec": {}}, objective_id, "")
        assert result is None

    def test_integration_goal_includes_branches(self, dispatcher, gw, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[
                TaskNode(
                    node_id="task_a", status="completed",
                    branch="composer/branch-a", commit_sha="sha_a_b2cd1234",
                ),
                TaskNode(
                    node_id="task_b", status="completed",
                    branch="composer/branch-b", commit_sha="sha_b_3f2e9876",
                ),
            ],
            integration=IntegrationNode(required=True, dependencies=["task_a", "task_b"]),
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        dispatcher.dispatch_integration(plan, {"normalized_spec": {}}, objective_id, "")

        # The integration task should have been dispatched via gw
        tasks = [t for t in gw._tasks.values() if t.metadata.get("composer_node_id") == "integration"]
        assert len(tasks) == 1
        dep_branches = tasks[0].metadata.get("dependency_branches", [])
        assert len(dep_branches) == 2
        branch_map = {d["node_id"]: d for d in dep_branches}
        assert branch_map["task_a"]["branch"] == "composer/branch-a"
        assert branch_map["task_b"]["branch"] == "composer/branch-b"

    def test_integration_runs_after_deps(self, dispatcher, gw, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[
                TaskNode(node_id="a", status="completed", branch="b_a", commit_sha="c_a"),
                TaskNode(node_id="b", status="completed", branch="b_b", commit_sha="c_b"),
                TaskNode(node_id="c", status="running"),  # not complete yet
            ],
            integration=IntegrationNode(required=True, dependencies=["a", "b"]),
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        result = dispatcher.dispatch_integration(plan, {"normalized_spec": {}}, objective_id, "")
        assert result is not None  # integration only depends on a and b, not c

    def test_final_branch_and_commit_stored(self, dispatcher, gw, cstorage, objective_id):
        spec = cstorage.create_spec(objective_id, "Test", "raw")
        plan = ComposerPlan(
            id="plan_1", objective_id=objective_id, spec_id=spec["id"],
            tasks=[
                TaskNode(node_id="a", status="completed", branch="b_a", commit_sha="c_a"),
            ],
            integration=IntegrationNode(required=True, dependencies=["a"]),
        )
        cstorage.create_plan(objective_id, spec["id"], plan)
        dispatcher.dispatch_integration(plan, {"normalized_spec": {}}, objective_id, "")

        # Check plan task row was updated
        pt = cstorage.get_plan_task_by_node("plan_1", "integration")
        assert pt["agents_gateway_task_id"] is not None
        assert pt["status"] == "running"
