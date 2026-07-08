"""Circuit breakers — hard safety limits for objective runs.

When a breaker trips:
1. Pause or block the objective/run
2. Emit event
3. Create approval or escalation item
4. Do not dispatch more work

Breakers:
- max_iterations_per_run
- max_cost_usd_per_run
- max_concurrent_tasks
- max_retries_per_task
- max_wall_clock_minutes
- max_stall_minutes
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional

from conductor.storage import ConductorStorage


@dataclass
class CircuitBreakerResult:
    tripped: bool
    breaker_name: str
    reason: str
    limit_value: float | int
    current_value: float | int


def evaluate_cost_breaker(
    storage: ConductorStorage,
    run_id: str,
    max_cost_usd: float,
) -> CircuitBreakerResult:
    total = storage.total_cost_for_run(run_id)
    tripped = total >= max_cost_usd
    return CircuitBreakerResult(
        tripped=tripped,
        breaker_name="max_cost_usd",
        reason=f"Cost ${total:.4f} exceeds max ${max_cost_usd:.2f}" if tripped else f"Cost ${total:.4f} within limit ${max_cost_usd:.2f}",
        limit_value=max_cost_usd,
        current_value=total,
    )


def evaluate_iteration_breaker(
    storage: ConductorStorage,
    run_id: str,
    max_iterations: int,
) -> CircuitBreakerResult:
    count = storage.count_planner_turns_for_run(run_id)
    tripped = count >= max_iterations
    return CircuitBreakerResult(
        tripped=tripped,
        breaker_name="max_iterations",
        reason=f"Iterations {count} exceeds max {max_iterations}" if tripped else f"Iterations {count} within limit {max_iterations}",
        limit_value=max_iterations,
        current_value=count,
    )


def evaluate_concurrency_breaker(
    storage: ConductorStorage,
    run_id: str,
    max_concurrent: int,
) -> CircuitBreakerResult:
    agent_count = storage.count_agent_runs_for_run(run_id)
    tripped = agent_count >= max_concurrent
    return CircuitBreakerResult(
        tripped=tripped,
        breaker_name="max_concurrent_tasks",
        reason=f"Agent runs {agent_count} exceeds max concurrent {max_concurrent}" if tripped else f"Agent runs {agent_count} within limit {max_concurrent}",
        limit_value=max_concurrent,
        current_value=agent_count,
    )


def evaluate_retry_breaker(
    task_attempt_count: int,
    max_retries: int,
) -> CircuitBreakerResult:
    tripped = task_attempt_count > max_retries
    return CircuitBreakerResult(
        tripped=tripped,
        breaker_name="max_retries",
        reason=f"Retry {task_attempt_count} exceeds max retries {max_retries}" if tripped else f"Retry {task_attempt_count} within limit {max_retries}",
        limit_value=max_retries,
        current_value=task_attempt_count,
    )


def evaluate_wall_clock_breaker(
    started_at_iso: str | None,
    max_minutes: int,
) -> CircuitBreakerResult:
    if not started_at_iso:
        return CircuitBreakerResult(
            tripped=False,
            breaker_name="max_wall_clock",
            reason="No start time recorded",
            limit_value=max_minutes,
            current_value=0,
        )
    start = datetime.fromisoformat(started_at_iso.replace("Z", "+00:00"))
    elapsed = (datetime.now(UTC) - start.replace(tzinfo=UTC)).total_seconds() / 60
    tripped = elapsed >= max_minutes
    return CircuitBreakerResult(
        tripped=tripped,
        breaker_name="max_wall_clock",
        reason=f"Elapsed {elapsed:.1f}m exceeds max {max_minutes}m" if tripped else f"Elapsed {elapsed:.1f}m within {max_minutes}m",
        limit_value=max_minutes,
        current_value=elapsed,
    )


def evaluate_stall_breaker(
    last_activity_at_iso: str,
    max_stall_minutes: int,
) -> CircuitBreakerResult:
    last = datetime.fromisoformat(last_activity_at_iso.replace("Z", "+00:00"))
    stale = (datetime.now(UTC) - last.replace(tzinfo=UTC)).total_seconds() / 60
    tripped = stale >= max_stall_minutes
    return CircuitBreakerResult(
        tripped=tripped,
        breaker_name="max_stall",
        reason=f"Stale {stale:.1f}m exceeds max {max_stall_minutes}m" if tripped else f"Stale {stale:.1f}m within {max_stall_minutes}m",
        limit_value=max_stall_minutes,
        current_value=stale,
    )


class BreakerEvaluator:
    def __init__(
        self,
        storage: ConductorStorage,
        max_iterations: int = 50,
        max_cost_usd: float = 10.0,
        max_concurrent: int = 4,
        max_retries: int = 3,
        max_wall_clock: int = 120,
        max_stall: int = 30,
    ) -> None:
        self.storage = storage
        self.max_iterations = max_iterations
        self.max_cost_usd = max_cost_usd
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.max_wall_clock = max_wall_clock
        self.max_stall = max_stall

    def evaluate_all_for_run(self, run_id: str, started_at: str | None = None, last_activity: str | None = None) -> list[CircuitBreakerResult]:
        results: list[CircuitBreakerResult] = []
        results.append(evaluate_cost_breaker(self.storage, run_id, self.max_cost_usd))
        results.append(evaluate_iteration_breaker(self.storage, run_id, self.max_iterations))
        results.append(evaluate_concurrency_breaker(self.storage, run_id, self.max_concurrent))
        if started_at:
            results.append(evaluate_wall_clock_breaker(started_at, self.max_wall_clock))
        if last_activity:
            results.append(evaluate_stall_breaker(last_activity, self.max_stall))
        return results

    def any_tripped(self, run_id: str, started_at: str | None = None, last_activity: str | None = None) -> list[CircuitBreakerResult]:
        results = self.evaluate_all_for_run(run_id, started_at=started_at, last_activity=last_activity)
        return [r for r in results if r.tripped]

    def evaluate_retry(self, attempt_count: int) -> CircuitBreakerResult:
        return evaluate_retry_breaker(attempt_count, self.max_retries)

    def can_dispatch(self, run_id: str, started_at: str | None = None, last_activity: str | None = None) -> tuple[bool, list[CircuitBreakerResult]]:
        tripped = self.any_tripped(run_id, started_at=started_at, last_activity=last_activity)
        return len(tripped) == 0, tripped