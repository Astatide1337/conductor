"""Tests for storage initialization, CRUD, and state machine validation."""

import os
import tempfile

import pytest

from conductor.storage import (
    ConductorStorage,
    TransitionError,
    OBJECTIVE_TRANSITIONS,
    TASK_TRANSITIONS,
    AGENT_RUN_TRANSITIONS,
    validate_transition,
)


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        s = ConductorStorage(db_path)
        s.initialize()
        yield s


@pytest.fixture
def objective(storage):
    return storage.create_objective(title="Test Objective", description="A test")


@pytest.fixture
def run(storage, objective):
    return storage.create_run(objective["id"])


@pytest.fixture
def task(storage, objective, run):
    return storage.create_task(objective["id"], run["id"], "Test Task", brief="do something")


class TestStorageInit:
    def test_initialize_creates_db(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "test.db")
            s = ConductorStorage(db_path)
            s.initialize()
            assert os.path.isfile(db_path)

    def test_initialize_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "test.db")
            s = ConductorStorage(db_path)
            s.initialize()
            s.initialize()  # should not raise


class TestObjectives:
    def test_create(self, storage):
        obj = storage.create_objective(title="Build Auth")
        assert obj["id"]
        assert obj["title"] == "Build Auth"
        assert obj["status"] == "created"
        assert obj["priority"] == "normal"
        assert "created_at" in obj

    def test_get(self, storage, objective):
        fetched = storage.get_objective(objective["id"])
        assert fetched is not None
        assert fetched["title"] == objective["title"]

    def test_get_nonexistent(self, storage):
        assert storage.get_objective("nonexistent") is None

    def test_list(self, storage):
        storage.create_objective(title="A")
        storage.create_objective(title="B")
        objs = storage.list_objectives()
        assert len(objs) >= 2

    def test_list_by_status(self, storage):
        storage.create_objective(title="Active")
        objs = storage.list_objectives(status="created")
        assert all(o["status"] == "created" for o in objs)


class TestObjectiveRuns:
    def test_create_run(self, storage, objective):
        run = storage.create_run(objective["id"])
        assert run["id"]
        assert run["objective_id"] == objective["id"]
        assert run["status"] == "created"
        assert run["planner_mode"] == "manual"

    def test_create_run_bad_objective(self, storage):
        run = storage.create_run("nonexistent")
        assert run is None

    def test_get_run(self, storage, run):
        fetched = storage.get_run(run["id"])
        assert fetched["id"] == run["id"]

    def test_list_runs(self, storage, objective):
        storage.create_run(objective["id"])
        storage.create_run(objective["id"])
        runs = storage.list_runs(objective["id"])
        assert len(runs) == 2


class TestTasks:
    def test_create_task(self, storage, task):
        assert task["id"]
        assert task["title"] == "Test Task"
        assert task["status"] == "created"
        assert task["task_type"] == "ship"

    def test_create_task_with_skills(self, storage, objective, run):
        t = storage.create_task(
            objective["id"], run["id"], "Scout Task",
            task_type="scout", required_skills=["code-review", "testing"],
        )
        assert t["required_skills"] == ["code-review", "testing"]
        assert t["task_type"] == "scout"

    def test_get_task(self, storage, task):
        fetched = storage.get_task(task["id"])
        assert fetched["title"] == task["title"]

    def test_list_tasks_by_objective(self, storage, objective, run):
        storage.create_task(objective["id"], run["id"], "T1")
        storage.create_task(objective["id"], run["id"], "T2")
        tasks = storage.list_tasks(objective_id=objective["id"])
        assert len(tasks) == 2

    def test_list_tasks_by_run(self, storage, objective, run):
        storage.create_task(objective["id"], run["id"], "T1")
        tasks = storage.list_tasks(run_id=run["id"])
        assert len(tasks) >= 1


class TestObjectiveTransitions:
    def test_created_to_active(self, storage, objective):
        updated = storage.update_objective_status(objective["id"], "active")
        assert updated["status"] == "active"

    def test_active_to_paused(self, storage, objective):
        storage.update_objective_status(objective["id"], "active")
        updated = storage.update_objective_status(objective["id"], "paused")
        assert updated["status"] == "paused"

    def test_paused_to_active(self, storage, objective):
        storage.update_objective_status(objective["id"], "active")
        storage.update_objective_status(objective["id"], "paused")
        updated = storage.update_objective_status(objective["id"], "active")
        assert updated["status"] == "active"

    def test_active_to_completed(self, storage, objective):
        storage.update_objective_status(objective["id"], "active")
        updated = storage.update_objective_status(objective["id"], "completed")
        assert updated["status"] == "completed"

    def test_created_direct_to_completed_invalid(self, storage, objective):
        with pytest.raises(TransitionError, match="Invalid transition"):
            storage.update_objective_status(objective["id"], "completed")

    def test_created_direct_to_cancelled_invalid(self, storage, objective):
        with pytest.raises(TransitionError, match="Invalid transition"):
            storage.update_objective_status(objective["id"], "cancelled")

    def test_completed_terminal(self, storage, objective):
        storage.update_objective_status(objective["id"], "active")
        storage.update_objective_status(objective["id"], "completed")
        with pytest.raises(TransitionError, match="Valid targets.*none"):
            storage.update_objective_status(objective["id"], "active")

    def test_failed_terminal(self, storage, objective):
        storage.update_objective_status(objective["id"], "active")
        storage.update_objective_status(objective["id"], "failed")
        with pytest.raises(TransitionError):
            storage.update_objective_status(objective["id"], "active")

    def test_active_to_blocked(self, storage, objective):
        storage.update_objective_status(objective["id"], "active")
        updated = storage.update_objective_status(objective["id"], "blocked")
        assert updated["status"] == "blocked"

    def test_blocked_to_active(self, storage, objective):
        storage.update_objective_status(objective["id"], "active")
        storage.update_objective_status(objective["id"], "blocked")
        updated = storage.update_objective_status(objective["id"], "active")
        assert updated["status"] == "active"

    def test_blocked_to_failed(self, storage, objective):
        storage.update_objective_status(objective["id"], "active")
        storage.update_objective_status(objective["id"], "blocked")
        updated = storage.update_objective_status(objective["id"], "failed")
        assert updated["status"] == "failed"


class TestTaskTransitions:
    def test_created_to_ready(self, storage, task):
        updated = storage.update_task_status(task["id"], "ready")
        assert updated["status"] == "ready"

    def test_ready_to_dispatched(self, storage, task):
        storage.update_task_status(task["id"], "ready")
        updated = storage.update_task_status(task["id"], "dispatched")
        assert updated["status"] == "dispatched"

    def test_dispatched_to_running(self, storage, task):
        storage.update_task_status(task["id"], "ready")
        storage.update_task_status(task["id"], "dispatched")
        updated = storage.update_task_status(task["id"], "running")
        assert updated["status"] == "running"

    def test_running_to_completed(self, storage, task):
        storage.update_task_status(task["id"], "ready")
        storage.update_task_status(task["id"], "dispatched")
        storage.update_task_status(task["id"], "running")
        updated = storage.update_task_status(task["id"], "completed")
        assert updated["status"] == "completed"

    def test_created_direct_to_running_invalid(self, storage, task):
        with pytest.raises(TransitionError, match="Invalid transition"):
            storage.update_task_status(task["id"], "running")

    def test_ready_to_cancelled(self, storage, task):
        storage.update_task_status(task["id"], "ready")
        updated = storage.update_task_status(task["id"], "cancelled")
        assert updated["status"] == "cancelled"

    def test_running_to_blocked(self, storage, task):
        storage.update_task_status(task["id"], "ready")
        storage.update_task_status(task["id"], "dispatched")
        storage.update_task_status(task["id"], "running")
        updated = storage.update_task_status(task["id"], "blocked")
        assert updated["status"] == "blocked"

    def test_blocked_to_ready(self, storage, task):
        storage.update_task_status(task["id"], "ready")
        storage.update_task_status(task["id"], "dispatched")
        storage.update_task_status(task["id"], "running")
        storage.update_task_status(task["id"], "blocked")
        updated = storage.update_task_status(task["id"], "ready")
        assert updated["status"] == "ready"

    def test_completed_terminal(self, storage, task):
        storage.update_task_status(task["id"], "ready")
        storage.update_task_status(task["id"], "dispatched")
        storage.update_task_status(task["id"], "running")
        storage.update_task_status(task["id"], "completed")
        with pytest.raises(TransitionError):
            storage.update_task_status(task["id"], "running")


class TestTransitionValidator:
    def test_validate_good_transition(self):
        assert validate_transition(TASK_TRANSITIONS, "created", "ready")

    def test_validate_bad_transition_raises(self):
        with pytest.raises(TransitionError):
            validate_transition(TASK_TRANSITIONS, "created", "running")

    def test_validate_terminal_rejects(self):
        with pytest.raises(TransitionError):
            validate_transition(TASK_TRANSITIONS, "completed", "running")


class TestAgentRuns:
    def test_create_agent_run(self, storage, task):
        ar = storage.create_agent_run(
            task["id"], idempotency_key="obj:run:task:1", dispatch_profile="default"
        )
        assert ar["id"]
        assert ar["status"] == "created"
        assert ar["idempotency_key"] == "obj:run:task:1"
        assert ar["attempt"] == 1

    def test_idempotency_key_lookup(self, storage, task):
        key = "obj:run:task:1"
        storage.create_agent_run(task["id"], idempotency_key=key)
        found = storage.get_agent_run_by_idempotency(key)
        assert found is not None
        assert found["idempotency_key"] == key

    def test_idempotency_key_not_found(self, storage):
        assert storage.get_agent_run_by_idempotency("nonexistent") is None

    def test_agent_run_transitions(self, storage, task):
        ar = storage.create_agent_run(task["id"])
        ar = storage.update_agent_run_status(ar["id"], "dispatched")
        assert ar["status"] == "dispatched"

    def test_agent_run_invalid_transition(self, storage, task):
        ar = storage.create_agent_run(task["id"])
        with pytest.raises(TransitionError):
            storage.update_agent_run_status(ar["id"], "completed")  # created->completed invalid


class TestApprovals:
    def test_create_approval(self, storage, objective, run):
        approval = storage.create_approval(
            objective["id"], run["id"], "deploy_production",
            description="Deploy to production", risk_level="high",
        )
        assert approval["id"]
        assert approval["status"] == "pending"
        assert approval["action_type"] == "deploy_production"

    def test_approval_list(self, storage, objective, run):
        storage.create_approval(objective["id"], run["id"], "merge_main")
        storage.create_approval(objective["id"], run["id"], "delete_data")
        approvals = storage.list_approvals(objective_id=objective["id"])
        assert len(approvals) == 2

    def test_approval_approve(self, storage, objective, run):
        approval = storage.create_approval(objective["id"], run["id"], "merge_main")
        updated = storage.update_approval_status(approval["id"], "approved", decided_by="alice", decision_reason="Reviewed")
        assert updated["status"] == "approved"
        assert updated["decided_by"] == "alice"
        assert updated["decided_at"] is not None

    def test_approval_reject(self, storage, objective, run):
        approval = storage.create_approval(objective["id"], run["id"], "merge_main")
        updated = storage.update_approval_status(approval["id"], "rejected", decided_by="bob")
        assert updated["status"] == "rejected"

    def test_approval_terminal(self, storage, objective, run):
        approval = storage.create_approval(objective["id"], run["id"], "merge_main")
        storage.update_approval_status(approval["id"], "approved")
        with pytest.raises(TransitionError):
            storage.update_approval_status(approval["id"], "rejected")


class TestCostLedger:
    def test_record_cost(self, storage, objective, run):
        entry = storage.record_cost(objective["id"], run["id"], source="planner", amount_usd=0.05, tokens_in=100, tokens_out=50)
        assert entry["amount_usd"] == 0.05

    def test_total_cost(self, storage, objective, run):
        storage.record_cost(objective["id"], run["id"], amount_usd=0.01)
        storage.record_cost(objective["id"], run["id"], amount_usd=0.02)
        total = storage.total_cost_for_run(run["id"])
        assert total == 0.03

    def test_total_cost_empty(self, storage, objective, run):
        assert storage.total_cost_for_run(run["id"]) == 0.0


class TestPlannerTurns:
    def test_record_planner_turn(self, storage, objective, run):
        turn = storage.record_planner_turn(
            objective["id"], run["id"],
            input_summary="test", output={"decision": "dispatch"},
            model="test-model", tokens_in=50, tokens_out=20, cost_usd=0.01,
        )
        assert turn["valid"] is True
        assert turn["model"] == "test-model"
        assert turn["cost_usd"] == 0.01

    def test_count_turns(self, storage, objective, run):
        storage.record_planner_turn(objective["id"], run["id"])
        storage.record_planner_turn(objective["id"], run["id"])
        assert storage.count_planner_turns_for_run(run["id"]) == 2


class TestPersistence:
    def test_restart_preserves_state(self, storage, objective, run, task):
        db_path = storage.db_path
        objective_id = objective["id"]
        run_id = run["id"]
        task_id = task["id"]
        storage.update_objective_status(objective_id, "active")
        storage.update_task_status(task_id, "ready")

        s2 = ConductorStorage(db_path)
        s2.initialize()
        fetched_obj = s2.get_objective(objective_id)
        fetched_task = s2.get_task(task_id)
        assert fetched_obj["status"] == "active"
        assert fetched_task["status"] == "ready"