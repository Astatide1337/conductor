"""Tests for circuit breakers — hard safety limits."""

import os
import tempfile
import time

import pytest

from conductor.storage import ConductorStorage
from conductor.circuit import (
    BreakerEvaluator,
    evaluate_cost_breaker,
    evaluate_iteration_breaker,
    evaluate_concurrency_breaker,
    evaluate_retry_breaker,
    evaluate_wall_clock_breaker,
    evaluate_stall_breaker,
)


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        s = ConductorStorage(db_path)
        s.initialize()
        yield s


@pytest.fixture
def populated(storage):
    obj = storage.create_objective(title="Breaker Test")
    run = storage.create_run(obj["id"], max_iterations=5, max_cost_usd=5.0, max_concurrent_tasks=2)
    return storage, obj, run


class TestCostBreaker:
    def test_within_limit(self, storage, populated):
        s, obj, run = populated
        result = evaluate_cost_breaker(s, run["id"], max_cost_usd=5.0)
        assert result.tripped is False
        assert result.current_value == 0.0

    def test_exceeds_limit(self, storage, populated):
        s, obj, run = populated
        s.record_cost(obj["id"], run["id"], amount_usd=6.0)
        result = evaluate_cost_breaker(s, run["id"], max_cost_usd=5.0)
        assert result.tripped is True
        assert result.current_value == 6.0
        assert "exceeds" in result.reason.lower()

    def test_at_limit(self, storage, populated):
        s, obj, run = populated
        s.record_cost(obj["id"], run["id"], amount_usd=5.0)
        result = evaluate_cost_breaker(s, run["id"], max_cost_usd=5.0)
        assert result.tripped is True  # >= limit


class TestIterationBreaker:
    def test_within_limit(self, storage, populated):
        s, obj, run = populated
        s.record_planner_turn(obj["id"], run["id"])
        s.record_planner_turn(obj["id"], run["id"])
        result = evaluate_iteration_breaker(s, run["id"], max_iterations=5)
        assert result.tripped is False

    def test_exceeds_limit(self, storage, populated):
        s, obj, run = populated
        for _ in range(6):
            s.record_planner_turn(obj["id"], run["id"])
        result = evaluate_iteration_breaker(s, run["id"], max_iterations=5)
        assert result.tripped is True
        assert result.current_value == 6


class TestConcurrencyBreaker:
    def test_within_limit(self, storage, populated):
        s, obj, run = populated
        task = s.create_task(obj["id"], run["id"], "T1")
        s.create_agent_run(task["id"])
        result = evaluate_concurrency_breaker(s, run["id"], max_concurrent=2)
        assert result.tripped is False
        assert result.current_value == 1

    def test_exceeds_limit(self, storage, populated):
        s, obj, run = populated
        for i in range(3):
            task = s.create_task(obj["id"], run["id"], f"T{i}")
            s.create_agent_run(task["id"])
        result = evaluate_concurrency_breaker(s, run["id"], max_concurrent=2)
        assert result.tripped is True
        assert result.current_value == 3


class TestRetryBreaker:
    def test_within_limit(self):
        result = evaluate_retry_breaker(task_attempt_count=3, max_retries=3)
        assert result.tripped is False

    def test_exceeds_limit(self):
        result = evaluate_retry_breaker(task_attempt_count=4, max_retries=3)
        assert result.tripped is True

    def test_first_attempt(self):
        result = evaluate_retry_breaker(task_attempt_count=1, max_retries=3)
        assert result.tripped is False


class TestWallClockBreaker:
    def test_no_start_time(self):
        result = evaluate_wall_clock_breaker(None, max_minutes=10)
        assert result.tripped is False

    def test_within_limit(self):
        from datetime import UTC, datetime, timedelta
        started = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        result = evaluate_wall_clock_breaker(started, max_minutes=10)
        assert result.tripped is False
        assert result.current_value >= 4.9  # ~5 min elapsed

    def test_exceeds_limit(self):
        from datetime import UTC, datetime, timedelta
        started = (datetime.now(UTC) - timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
        result = evaluate_wall_clock_breaker(started, max_minutes=10)
        assert result.tripped is True


class TestStallBreaker:
    def test_within_limit(self):
        from datetime import UTC, datetime, timedelta
        last = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        result = evaluate_stall_breaker(last, max_stall_minutes=30)
        assert result.tripped is False

    def test_exceeds_limit(self):
        from datetime import UTC, datetime, timedelta
        last = (datetime.now(UTC) - timedelta(minutes=40)).isoformat().replace("+00:00", "Z")
        result = evaluate_stall_breaker(last, max_stall_minutes=30)
        assert result.tripped is True


class TestBreakerEvaluator:
    def test_evaluate_all(self, storage, populated):
        s, obj, run = populated
        evaluator = BreakerEvaluator(s, max_iterations=5, max_cost_usd=5.0, max_concurrent=2)
        results = evaluator.evaluate_all_for_run(run["id"])
        assert len(results) >= 3  # cost, iteration, concurrency

    def test_no_trips_on_clean_run(self, storage, populated):
        s, obj, run = populated
        evaluator = BreakerEvaluator(s, max_iterations=100, max_cost_usd=100.0, max_concurrent=10)
        tripped = evaluator.any_tripped(run["id"])
        assert len(tripped) == 0

    def test_can_dispatch_true(self, storage, populated):
        s, obj, run = populated
        evaluator = BreakerEvaluator(s, max_iterations=100, max_cost_usd=100.0, max_concurrent=10)
        ok, tripped = evaluator.can_dispatch(run["id"])
        assert ok is True
        assert len(tripped) == 0

    def test_can_dispatch_false_on_cost(self, storage, populated):
        s, obj, run = populated
        s.record_cost(obj["id"], run["id"], amount_usd=100.0)
        evaluator = BreakerEvaluator(s, max_cost_usd=5.0)
        ok, tripped = evaluator.can_dispatch(run["id"])
        assert ok is False
        assert any(r.breaker_name == "max_cost_usd" for r in tripped)

    def test_can_dispatch_false_on_concurrency(self, storage, populated):
        s, obj, run = populated
        for i in range(5):
            task = s.create_task(obj["id"], run["id"], f"Concurrent{i}")
            s.create_agent_run(task["id"])
        evaluator = BreakerEvaluator(s, max_concurrent=2)
        ok, tripped = evaluator.can_dispatch(run["id"])
        assert ok is False
        assert any(r.breaker_name == "max_concurrent_tasks" for r in tripped)

    def test_evaluate_retry(self):
        evaluator = BreakerEvaluator(None, max_retries=3)  # type: ignore
        result = evaluator.evaluate_retry(4)
        assert result.tripped is True