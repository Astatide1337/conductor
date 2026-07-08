"""Tests for deterministic planner — rule-based automation."""

import os
import tempfile

import pytest

from conductor.storage import ConductorStorage
from conductor.circuit import BreakerEvaluator
from conductor.planner.deterministic import (
    run_deterministic_planner,
    run_dry_run,
    PlannerDecision,
)
from conductor.clients.skills_gateway import MockSkillsGatewayClient


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        s = ConductorStorage(db_path)
        s.initialize()
        yield s


@pytest.fixture
def setup(storage):
    obj = storage.create_objective(title="Planner Test")
    run = storage.create_run(obj["id"], max_iterations=10, max_cost_usd=10.0, max_concurrent_tasks=2)
    breakers = BreakerEvaluator(storage, max_iterations=10, max_cost_usd=10.0, max_concurrent=2, max_retries=3)
    skills = MockSkillsGatewayClient()
    skills.register("code-review", "Code Review")
    return storage, obj, run, breakers, skills


def _make_task(storage, objective_id, run_id, title="T", status="ready", task_type="ship"):
    task = storage.create_task(objective_id, run_id, title, task_type=task_type)
    if status == "ready":
        storage.update_task_status(task["id"], "ready")
    elif status == "dispatched":
        storage.update_task_status(task["id"], "ready")
        storage.update_task_status(task["id"], "dispatched")
    elif status == "running":
        storage.update_task_status(task["id"], "ready")
        storage.update_task_status(task["id"], "dispatched")
        storage.update_task_status(task["id"], "running")
    elif status == "completed":
        storage.update_task_status(task["id"], "ready")
        storage.update_task_status(task["id"], "dispatched")
        storage.update_task_status(task["id"], "running")
        storage.update_task_status(task["id"], "completed")
    elif status == "failed":
        storage.update_task_status(task["id"], "ready")
        storage.update_task_status(task["id"], "dispatched")
        storage.update_task_status(task["id"], "running")
        storage.update_task_status(task["id"], "failed")
    return storage.get_task(task["id"])


class TestDeterministicPlanner:
    def test_no_tasks_needs_creation(self, setup):
        s, obj, run, breakers, skills = setup
        decision = run_deterministic_planner(s, run["id"], breakers)
        assert decision is not None
        assert decision.decision_type == "create_tasks"

    def test_dispatch_ready_task(self, setup):
        s, obj, run, breakers, skills = setup
        _make_task(s, obj["id"], run["id"], "Ready Task", status="ready")
        decision = run_deterministic_planner(s, run["id"], breakers)
        assert decision is not None
        assert decision.decision_type == "dispatch_task"
        assert decision.task_id is not None

    def test_concurrency_limit(self, setup):
        s, obj, run, breakers, skills = setup
        _make_task(s, obj["id"], run["id"], "T1", status="ready")
        for i in range(2):
            t = s.create_task(obj["id"], run["id"], f"Agent{i}")
            s.create_agent_run(t["id"])
        decision = run_deterministic_planner(s, run["id"], breakers)
        # With 2 agent_runs at max_concurrent=2, breaker trips
        assert decision.decision_type == "request_approval"

    def test_all_completed(self, setup):
        s, obj, run, breakers, skills = setup
        _make_task(s, obj["id"], run["id"], "Completed", status="completed")
        decision = run_deterministic_planner(s, run["id"], breakers)
        assert decision is not None
        assert decision.decision_type == "mark_objective_complete"

    def test_all_failed(self, setup):
        s, obj, run, breakers, skills = setup
        _make_task(s, obj["id"], run["id"], "Failed Task", status="failed")
        decision = run_deterministic_planner(s, run["id"], breakers)
        assert decision.decision_type == "mark_objective_blocked"

    def test_created_task_moved_to_ready(self, setup):
        s, obj, run, breakers, skills = setup
        _make_task(s, obj["id"], run["id"], "New Task", status="created")
        decision = run_deterministic_planner(s, run["id"], breakers)
        assert decision is not None

    def test_blocked_task_unblocked(self, setup):
        s, obj, run, breakers, skills = setup
        t = s.create_task(obj["id"], run["id"], "Blocked Task")
        s.update_task_status(t["id"], "ready")
        s.update_task_status(t["id"], "dispatched")
        s.update_task_status(t["id"], "running")
        s.update_task_status(t["id"], "blocked")
        decision = run_deterministic_planner(s, run["id"], breakers)
        assert decision is not None  # should move to ready

    def test_paused_objective_does_nothing(self, setup):
        s, obj, run, breakers, skills = setup
        s.update_objective_status(obj["id"], "active")
        s.update_objective_status(obj["id"], "paused")
        decision = run_deterministic_planner(s, run["id"], breakers)
        assert decision.decision_type == "do_nothing"

    def test_cost_breaker_trips(self, setup):
        s, obj, run, breakers, skills = setup
        _make_task(s, obj["id"], run["id"], "Ready Task", status="ready")
        s.record_cost(obj["id"], run["id"], amount_usd=20.0)  # exceed 10.0 limit
        decision = run_deterministic_planner(s, run["id"], breakers)
        assert decision.decision_type == "request_approval"


class TestDryRun:
    def test_dry_run_with_ready_task(self, setup):
        s, obj, run, breakers, skills = setup
        _make_task(s, obj["id"], run["id"], "Ready Task", status="ready")
        result = run_dry_run(s, run["id"], breakers, skills_client=skills)
        assert result.would_dispatch is True
        assert len(result.estimated_risks) >= 1

    def test_dry_run_no_tasks(self, setup):
        s, obj, run, breakers, skills = setup
        result = run_dry_run(s, run["id"], breakers)
        assert result.would_dispatch is False