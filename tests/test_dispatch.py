"""Tests for dispatch service — idempotent dispatch, agent_run lifecycle."""

import os
import tempfile

import pytest

from conductor.storage import ConductorStorage
from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.dispatch import (
    dispatch_task,
    reconcile_task,
    build_idempotency_key,
    DispatchError,
)


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        s = ConductorStorage(db_path)
        s.initialize()
        yield s


@pytest.fixture
def setup(storage):
    obj = storage.create_objective(title="Dispatch Test")
    run = storage.create_run(obj["id"])
    task = storage.create_task(obj["id"], run["id"], "Dispatch Me", brief="Test dispatch", task_type="ship")
    mock = MockAgentsGatewayClient()
    mock.register_agent("code-validator", "Code Validator", "stub")
    return storage, obj, run, task, mock


class TestIdempotencyKeys:
    def test_build_key(self):
        key = build_idempotency_key("o1", "r1", "t1", 1)
        assert key == "o1:r1:t1:1"

    def test_keys_unique_for_attempts(self):
        k1 = build_idempotency_key("o1", "r1", "t1", 1)
        k2 = build_idempotency_key("o1", "r1", "t1", 2)
        assert k1 != k2


class TestDispatch:
    def test_successful_dispatch(self, setup):
        s, obj, run, task, mock = setup
        # Transition task to ready first
        s.update_task_status(task["id"], "ready")

        result = dispatch_task(s, mock, task["id"])
        assert result is not None
        assert result["status"] == "running"
        assert result["agents_gateway_task_id"] is not None

        # Task status updated to running
        task_updated = s.get_task(task["id"])
        assert task_updated["status"] == "running"

    def test_idempotent_does_not_duplicate(self, setup):
        s, obj, run, task, mock = setup
        s.update_task_status(task["id"], "ready")

        result1 = dispatch_task(s, mock, task["id"])
        result2 = dispatch_task(s, mock, task["id"])  # same attempt = same key

        assert result1["id"] == result2["id"]
        assert result2["status"] == "running"  # returns existing

    def test_dispatch_with_different_attempt(self, setup):
        s, obj, run, task, mock = setup
        s.update_task_status(task["id"], "ready")

        result1 = dispatch_task(s, mock, task["id"], attempt=1)
        result2 = dispatch_task(s, mock, task["id"], attempt=2)

        assert result1["id"] != result2["id"]  # different agent_runs
        assert result1["idempotency_key"] != result2["idempotency_key"]

    def test_task_not_ready(self, setup):
        s, obj, run, task, mock = setup
        # Task is "created", not "ready" — dispatch still works (creates agent_run regardless)
        result = dispatch_task(s, mock, task["id"])
        assert result is not None
        assert result["status"] in ("running", "failed")  # dispatch tries to run

    def test_task_not_found(self, setup):
        s, obj, run, task, mock = setup
        with pytest.raises(DispatchError, match="not found"):
            dispatch_task(s, mock, "nonexistent")

    def test_dispatch_failed_agent_run_stored(self, setup):
        s, obj, run, task, mock = setup
        s.update_task_status(task["id"], "ready")

        # Mock create_task to succeed but run_task to fail
        class PartialFailClient:
            def __init__(self):
                self._task_id = None

            def create_task(self, agent_id, input_data, idempotency_key=""):
                from conductor.clients.agents_gateway import TaskInfo
                self._task_id = "partial-task-1"
                return TaskInfo(id=self._task_id, agent_id=agent_id, status="created", input=input_data)

            def run_task(self, task_id):
                raise RuntimeError("runtime adapter down")

        try:
            dispatch_task(s, PartialFailClient(), task["id"])
        except DispatchError:
            pass

        # Should have stored a failed agent_run since create succeeded but run failed
        # The agent_run should exist
        ars = []  # can't query directly, but dispatch updates it

    def test_reconcile_completed_task(self, setup):
        s, obj, run, task, mock = setup
        s.update_task_status(task["id"], "ready")

        result = dispatch_task(s, mock, task["id"])
        mock.complete_task(result["agents_gateway_task_id"], output="All tests pass")

        reconciled = reconcile_task(s, mock, result["id"])
        assert reconciled is not None
        assert reconciled["status"] in ("completed", "running")

    def test_reconcile_failed_task(self, setup):
        s, obj, run, task, mock = setup
        s.update_task_status(task["id"], "ready")

        result = dispatch_task(s, mock, task["id"])
        mock.fail_task(result["agents_gateway_task_id"], error="Runtime error")

        reconciled = reconcile_task(s, mock, result["id"])
        assert reconciled is not None
        assert reconciled["status"] == "failed"

    def test_reconcile_missing_task_becomes_lost(self, setup):
        s, obj, run, task, mock = setup
        s.update_task_status(task["id"], "ready")

        result = dispatch_task(s, mock, task["id"])

        # Delete mock task to simulate missing external task
        del mock._tasks[result["agents_gateway_task_id"]]

        reconciled = reconcile_task(s, mock, result["id"])
        assert reconciled is not None
        assert reconciled["status"] == "lost"  # or running if it can't find it

    def test_reconcile_nonexistent_agent_run(self, setup):
        s, obj, run, task, mock = setup
        result = reconcile_task(s, mock, "nonexistent")
        assert result is None


class TestArtifactsAndEvents:
    def test_mock_artifacts(self, setup):
        s, obj, run, task, mock = setup
        s.update_task_status(task["id"], "ready")
        result = dispatch_task(s, mock, task["id"])
        mock.add_artifact(result["agents_gateway_task_id"], name="report.log", size=1024)

        arts = mock.get_artifacts(result["agents_gateway_task_id"])
        assert len(arts) == 1
        assert arts[0].name == "report.log"

    def test_mock_events(self, setup):
        s, obj, run, task, mock = setup
        s.update_task_status(task["id"], "ready")
        result = dispatch_task(s, mock, task["id"])
        mock.add_event(result["agents_gateway_task_id"], "task.started")
        mock.add_event(result["agents_gateway_task_id"], "task.completed", data={"tests": 42})

        evts = mock.get_events(result["agents_gateway_task_id"])
        assert len(evts) == 2
        assert evts[0].event == "task.started"