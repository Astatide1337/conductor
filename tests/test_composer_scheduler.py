"""Tests for Composer scheduler — dispatch, dependencies, concurrency, conflicts."""

import pytest

from conductor.composer.models import (
    IntegrationNode,
    TaskNode,
    VerificationCommand,
    VerificationSpec,
)
from conductor.composer.scheduler import Scheduler, build_idempotency_key
from conductor.composer.storage import ComposerStorage
from conductor.storage import ConductorStorage
from conductor.clients.agents_gateway import MockAgentsGatewayClient


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
    # Register default harness
    client.register_harness_profile("pi-coding-agent", runnable=True)
    return client


@pytest.fixture
def scheduler(cstorage, gw, conductor_storage):
    return Scheduler(cstorage, gw, max_parallel_tasks=3, conductor_storage=conductor_storage)


class TestIdempotencyKey:
    def test_format(self):
        key = build_idempotency_key("obj_1", "plan_1", "task_a", 1)
        assert key == "composer:obj_1:plan_1:task_a:1"

    def test_different_attempts(self):
        k1 = build_idempotency_key("obj", "plan", "task", 1)
        k2 = build_idempotency_key("obj", "plan", "task", 2)
        assert k1 != k2


class TestFindReadyNodes:
    def test_no_dependencies_all_ready(self, scheduler):
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id="obj_1", spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a"),
                TaskNode(node_id="b"),
            ],
        )
        ready = scheduler.find_ready_nodes(plan)
        assert len(ready) == 2

    def test_dependency_prevents_ready(self, scheduler):
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id="obj_1", spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a"),
                TaskNode(node_id="b", dependencies=["a"]),
            ],
        )
        ready = scheduler.find_ready_nodes(plan)
        assert len(ready) == 1
        assert ready[0].node_id == "a"

    def test_dependency_completed_makes_ready(self, scheduler):
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id="obj_1", spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a", status="completed"),
                TaskNode(node_id="b", dependencies=["a"]),
            ],
        )
        ready = scheduler.find_ready_nodes(plan)
        assert len(ready) == 1
        assert ready[0].node_id == "b"

    def test_no_ready_when_all_completed(self, scheduler):
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id="obj_1", spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a", status="completed"),
                TaskNode(node_id="b", status="completed"),
            ],
        )
        ready = scheduler.find_ready_nodes(plan)
        assert ready == []

    def test_running_not_ready(self, scheduler):
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id="obj_1", spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a", status="running"),
                TaskNode(node_id="b"),
            ],
        )
        ready = scheduler.find_ready_nodes(plan)
        assert len(ready) == 1
        assert ready[0].node_id == "b"


class TestFindIntegrationReady:
    def test_integration_ready_all_deps_done(self, scheduler):
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id="obj_1", spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a", status="completed"),
                TaskNode(node_id="b", status="completed"),
            ],
            integration=IntegrationNode(required=True, dependencies=["a", "b"]),
        )
        assert scheduler.find_integration_ready(plan) is True

    def test_integration_not_ready_partial(self, scheduler):
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id="obj_1", spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a", status="completed"),
                TaskNode(node_id="b", status="running"),
            ],
            integration=IntegrationNode(required=True, dependencies=["a", "b"]),
        )
        assert scheduler.find_integration_ready(plan) is False

    def test_integration_not_ready_already_running(self, scheduler):
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id="obj_1", spec_id="spec_1",
            tasks=[
                TaskNode(node_id="a", status="completed"),
                TaskNode(node_id="b", status="completed"),
            ],
            integration=IntegrationNode(required=True, dependencies=["a", "b"], status="running"),
        )
        assert scheduler.find_integration_ready(plan) is False

    def test_no_integration_node(self, scheduler):
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id="obj_1", spec_id="spec_1",
            tasks=[TaskNode(node_id="a", status="completed")],
        )
        assert scheduler.find_integration_ready(plan) is False


class TestDispatchReadyTasks:
    def test_dispatch_ready(self, scheduler, gw, cstorage, conductor_storage):
        from conductor.composer.models import (
            ComposerPlan, ComposerPlan as CP,
        )
        # Create objective first
        obj = conductor_storage.create_objective(title="Test")
        from conductor.composer.models import ComposerPlan
        plan = ComposerPlan(
            id="plan_1", objective_id=obj["id"], spec_id="spec_1",
            tasks=[
                TaskNode(
                    node_id="a",
                    harness_profile="pi-coding-agent",
                    title="Task A",
                    goal="Do A",
                    verification=VerificationSpec(
                        commands=[VerificationCommand(name="t", command="t", required=True)],
                    ),
                ),
                TaskNode(
                    node_id="b",
                    harness_profile="pi-coding-agent",
                    title="Task B",
                    goal="Do B",
                    verification=VerificationSpec(
                        commands=[VerificationCommand(name="t", command="t", required=True)],
                    ),
                ),
            ],
            integration=IntegrationNode(dependencies=["a", "b"]),
        )
        # Create spec
        cstorage.create_spec(obj["id"], "Test", "raw")
        # Store plan
        cstorage.create_plan(obj["id"], "spec_1", plan)

        dispatched = scheduler.dispatch_ready_tasks(plan, {}, obj["id"], "", "master")
        assert len(dispatched) == 2

    def test_max_parallel_respected(self, cstorage, gw, conductor_storage):
        from conductor.composer.models import ComposerPlan
        scheduler = Scheduler(cstorage, gw, max_parallel_tasks=1, conductor_storage=conductor_storage)
        obj = conductor_storage.create_objective(title="Test")
        plan = ComposerPlan(
            id="plan_1", objective_id=obj["id"], spec_id="spec_1",
            tasks=[
                TaskNode(
                    node_id="a", harness_profile="pi-coding-agent",
                    goal="A",
                    verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)]),
                ),
                TaskNode(
                    node_id="b", harness_profile="pi-coding-agent",
                    goal="B",
                    verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)]),
                ),
            ],
        )
        cstorage.create_spec(obj["id"], "Test", "raw")
        cstorage.create_plan(obj["id"], "spec_1", plan)
        dispatched = scheduler.dispatch_ready_tasks(plan, {}, obj["id"], "", "master")
        assert len(dispatched) == 1  # only 1 due to max_parallel_tasks=1

    def test_dependencies_prevent_early_dispatch(self, scheduler, cstorage, conductor_storage):
        from conductor.composer.models import ComposerPlan
        obj = conductor_storage.create_objective(title="Test")
        plan = ComposerPlan(
            id="plan_1", objective_id=obj["id"], spec_id="spec_1",
            tasks=[
                TaskNode(
                    node_id="a", harness_profile="pi-coding-agent",
                    goal="A", status="running",
                    verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)]),
                ),
                TaskNode(
                    node_id="b", dependencies=["a"],
                    harness_profile="pi-coding-agent",
                    goal="B",
                    verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)]),
                ),
            ],
        )
        cstorage.create_spec(obj["id"], "Test", "raw")
        cstorage.create_plan(obj["id"], "spec_1", plan)
        dispatched = scheduler.dispatch_ready_tasks(plan, {}, obj["id"], "", "master")
        assert dispatched == []  # b depends on a which is running

    def test_file_scope_conflict(self, cstorage, gw, conductor_storage):
        from conductor.composer.models import ComposerPlan
        scheduler = Scheduler(cstorage, gw, max_parallel_tasks=3, conductor_storage=conductor_storage)
        obj = conductor_storage.create_objective(title="Test")
        plan = ComposerPlan(
            id="plan_1", objective_id=obj["id"], spec_id="spec_1",
            tasks=[
                TaskNode(
                    node_id="a", file_scope=["src/"], harness_profile="pi-coding-agent",
                    goal="A", status="running",
                    verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)]),
                ),
                TaskNode(
                    node_id="b", file_scope=["src/"], harness_profile="pi-coding-agent",
                    goal="B",
                    verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)]),
                ),
            ],
        )
        cstorage.create_spec(obj["id"], "Test", "raw")
        cstorage.create_plan(obj["id"], "spec_1", plan)
        dispatched = scheduler.dispatch_ready_tasks(plan, {}, obj["id"], "", "master")
        assert dispatched == []  # b conflicts with running a


class TestRestartFailedTask:
    def test_restart_with_context(self, scheduler, cstorage, conductor_storage):
        from conductor.composer.models import ComposerPlan
        obj = conductor_storage.create_objective(title="Test")
        node = TaskNode(
            node_id="a", harness_profile="pi-coding-agent",
            goal="Original goal",
            verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)]),
            status="failed",
        )
        plan = ComposerPlan(
            id="plan_1", objective_id=obj["id"], spec_id="spec_1",
            tasks=[node],
        )
        cstorage.create_spec(obj["id"], "Test", "raw")
        cstorage.create_plan(obj["id"], "spec_1", plan)
        result = scheduler.restart_failed_task(
            plan, node, {}, obj["id"], "", "master",
            failure_context="tests failed",
        )
        assert result is not None
        # The GW task's brief should include failure context, but the
        # durable node.goal must remain the exact planned text.
        gw_task = scheduler.agents_gateway.get_task(result["gw_task_id"])
        spec = gw_task.metadata.get("spec", {})
        goal_text = spec.get("goal", {}).get("text", "") if isinstance(spec.get("goal"), dict) else ""
        assert "Previous attempt failed" in goal_text
        assert node.goal == "Original goal"  # durable column unchanged

    def test_restart_no_context(self, scheduler, cstorage, conductor_storage):
        from conductor.composer.models import ComposerPlan
        obj = conductor_storage.create_objective(title="Test")
        node = TaskNode(
            node_id="a", harness_profile="pi-coding-agent",
            goal="Original",
            verification=VerificationSpec(commands=[VerificationCommand(name="t", command="t", required=True)]),
            status="failed",
        )
        plan = ComposerPlan(
            id="plan_1", objective_id=obj["id"], spec_id="spec_1",
            tasks=[node],
        )
        cstorage.create_spec(obj["id"], "Test", "raw")
        cstorage.create_plan(obj["id"], "spec_1", plan)
        result = scheduler.restart_failed_task(
            plan, node, {}, obj["id"], "", "master",
        )
        assert result is not None
        assert node.goal == "Original"
