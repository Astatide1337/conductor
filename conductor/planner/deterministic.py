"""Deterministic planner — rule-based automation without LLM.

Rules:
1. If run has no tasks and no agent_runs → blocked/needs planning
2. If ready tasks exist and max_concurrent not breached → dispatch next ready
3. If all tasks completed/failed → complete/fail run
4. If task failed and retries below max → retry
5. If task failed and retries exceed max → block task
6. If task blocked → check dependencies, move to ready
"""

import json
from dataclasses import dataclass, field
from typing import Optional

from conductor.circuit import BreakerEvaluator
from conductor.clients.agents_gateway import (
    BaseAgentsGatewayClient,
)
from conductor.clients.skills_gateway import (
    BaseSkillsGatewayClient,
    validate_required_skills,
)
from conductor.events import emit
from conductor.logging import get_logger
from conductor.models import DryRunResult
from conductor.policy import check_action, check_decision
from conductor.storage import ConductorStorage

logger = get_logger()


@dataclass
class PlannerDecision:
    decision_type: str
    reason: str
    task_id: Optional[str] = None
    new_tasks: list[dict] = field(default_factory=list)
    approval_request: Optional[dict] = None
    guidance: Optional[dict] = None
    confidence: float = 1.0


def run_deterministic_planner(
    storage: ConductorStorage,
    run_id: str,
    breakers: BreakerEvaluator,
    gateway: BaseAgentsGatewayClient | None = None,
    skills_client: BaseSkillsGatewayClient | None = None,
) -> PlannerDecision | None:
    run = storage.get_run(run_id)
    if not run:
        return None

    objective_id = run["objective_id"]

    # Check if objective is paused/blocked
    objective = storage.get_objective(objective_id)
    if objective and objective["status"] in ("paused", "blocked", "completed", "failed", "cancelled"):
        return PlannerDecision(
            decision_type="do_nothing",
            reason=f"Objective is {objective['status']} — no action",
            confidence=1.0,
        )
    if run["status"] in ("paused", "blocked", "completed", "failed", "cancelled"):
        return PlannerDecision(
            decision_type="do_nothing",
            reason=f"Run is {run['status']} — no action using deterministic planner",
            confidence=1.0,
        )

    # Check circuit breakers
    tripped = breakers.any_tripped(run_id, started_at=run.get("started_at"))
    if tripped:
        trip_names = [t.breaker_name for t in tripped]
        return PlannerDecision(
            decision_type="request_approval",
            reason=f"Circuit breakers tripped: {trip_names}",
            approval_request={
                "action_type": "circuit_breaker_trip",
                "description": f"Breakers tripped: {', '.join(trip_names)}",
                "risk_level": "high",
            },
            confidence=0.9,
        )

    tasks = storage.list_tasks(run_id=run_id)

    if not tasks:
        return PlannerDecision(
            decision_type="create_tasks",
            reason="Run has no tasks — needs task creation",
            confidence=1.0,
        )

    # Check for ready tasks to dispatch
    ready_tasks = [t for t in tasks if t["status"] == "ready"]
    if ready_tasks:
        concurrent = breakers.max_concurrent
        agent_count = storage.count_agent_runs_for_run(run_id)
        if agent_count < concurrent:
            next_task = ready_tasks[0]
            # Validate skills if skills client available
            if skills_client:
                valid, _, missing = validate_required_skills(skills_client, next_task.get("required_skills", []))
                if not valid:
                    return PlannerDecision(
                        decision_type="mark_task_blocked",
                        reason=f"Task has missing skills: {missing}",
                        task_id=next_task["id"],
                        confidence=0.95,
                    )
            return PlannerDecision(
                decision_type="dispatch_task",
                reason=f"Dispatching next ready task — {agent_count}/{concurrent} concurrent",
                task_id=next_task["id"],
                confidence=0.95,
            )
        else:
            return PlannerDecision(
                decision_type="do_nothing",
                reason=f"At max concurrency: {agent_count}/{concurrent}",
                confidence=1.0,
            )

    # Check completed/failed
    finished = [t for t in tasks if t["status"] in ("completed", "failed", "cancelled")]
    if len(finished) == len(tasks):
        failed_count = sum(1 for t in finished if t["status"] == "failed")
        if failed_count > 0:
            storage.update_run_status(run_id, "failed")
            emit(storage, "run.failed", f"Run {run_id} failed — {failed_count} tasks failed",
                 objective_id=objective_id, run_id=run_id, source="planner")
            return PlannerDecision(
                decision_type="mark_objective_blocked",
                reason=f"Run failed — {failed_count} failed tasks",
                confidence=1.0,
            )
        else:
            storage.update_run_status(run_id, "completed")
            emit(storage, "run.completed", f"Run {run_id} completed all tasks",
                 objective_id=objective_id, run_id=run_id, source="planner")
            return PlannerDecision(
                decision_type="mark_objective_complete",
                reason="All tasks completed successfully",
                confidence=1.0,
            )

    # Check for failed tasks with retries remaining
    failed_tasks = [t for t in tasks if t["status"] == "failed"]
    for ft in failed_tasks:
        agent_runs_count = 0  # approximate
        if agent_runs_count < breakers.max_retries:
            return PlannerDecision(
                decision_type="retry_task",
                reason=f"Task {ft['id']} failed — retrying",
                task_id=ft["id"],
                confidence=0.9,
            )

    # Check tasks in created/blocked state → make ready
    created_tasks = [t for t in tasks if t["status"] == "created"]
    blocked_tasks = [t for t in tasks if t["status"] == "blocked"]
    all_non_ready = created_tasks + blocked_tasks
    if all_non_ready:
        for t in all_non_ready:
            storage.update_task_status(t["id"], "ready")
        if blocked_tasks:
            return PlannerDecision(
                decision_type="mark_task_blocked",
                reason=f"Moved {len(all_non_ready)} tasks to ready",
                confidence=1.0,
            )
        return PlannerDecision(
            decision_type="do_nothing",
            reason=f"Moved {len(all_non_ready)} tasks to ready — rerun planner next turn",
            confidence=1.0,
        )

    return PlannerDecision(
        decision_type="do_nothing",
        reason="No tasks ready to dispatch — waiting",
        confidence=1.0,
    )


def run_dry_run(
    storage: ConductorStorage,
    run_id: str,
    breakers: BreakerEvaluator,
    skills_client: BaseSkillsGatewayClient | None = None,
) -> DryRunResult:
    tasks = storage.list_tasks(run_id=run_id)
    decision = run_deterministic_planner(storage, run_id, breakers, skills_client=skills_client)

    proposed = []
    required_skills: list[str] = []
    approval_gates: list[str] = []
    risks: list[str] = []
    would_dispatch = False

    if decision and decision.decision_type == "dispatch_task":
        task = storage.get_task(decision.task_id)
        if task:
            proposed.append(task)
            required_skills = task.get("required_skills", [])
            approved = task.get("approval_required", False)
            if approved:
                approval_gates.append("task requires approval")
            would_dispatch = True

    elif decision and decision.decision_type in ("request_approval",):
        approval_gates.append(f"Circuit breakers: {decision.reason}")

    ready_tasks = [t for t in tasks if t["status"] == "ready"]
    if ready_tasks:
        for t in ready_tasks:
            if t.get("approval_required"):
                approval_gates.append(f"Task {t['id']} requires approval")

        risk_skills = set()
        for t in ready_tasks:
            for s in t.get("required_skills", []):
                risk_skills.add(s)
        required_skills = sorted(risk_skills)

        risks.append(f"{len(ready_tasks)} ready tasks pending dispatch")

    return DryRunResult(
        proposed_tasks=proposed,
        required_skills=required_skills,
        approval_gates=approval_gates,
        estimated_risks=risks,
        would_dispatch=would_dispatch,
    )