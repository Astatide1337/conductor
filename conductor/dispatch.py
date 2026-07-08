"""Task dispatch coordination — sends work to Agents Gateway with idempotency protection."""

from datetime import UTC, datetime
from typing import Optional

from conductor.circuit import BreakerEvaluator
from conductor.clients.agents_gateway import (
    BaseAgentsGatewayClient,
    TaskInfo,
)
from conductor.events import emit
from conductor.logging import get_logger
from conductor.storage import ConductorStorage

logger = get_logger()


class DispatchError(Exception):
    pass


def build_idempotency_key(objective_id: str, run_id: str, task_id: str, attempt: int) -> str:
    return f"{objective_id}:{run_id}:{task_id}:{attempt}"


def dispatch_task(
    storage: ConductorStorage,
    gateway: BaseAgentsGatewayClient,
    task_id: str,
    agent_id: str = "code-validator",
    agent_input: str = "",
    dispatch_profile: str = "",
    attempt: int = 1,
) -> dict:
    task = storage.get_task(task_id)
    if not task:
        raise DispatchError(f"Task {task_id} not found")

    idem_key = build_idempotency_key(task["objective_id"], task["run_id"], task_id, attempt)

    existing = storage.get_agent_run_by_idempotency(idem_key)
    if existing:
        logger.info("idempotent_skip idempotency_key=%s status=%s", idem_key, existing["status"])
        return existing

    # Create agent_run record first
    agent_run = storage.create_agent_run(
        task_id,
        idempotency_key=idem_key,
        dispatch_profile=dispatch_profile or task.get("dispatch_profile", ""),
        runtime_type=task.get("task_type", "ship"),
    )

    # Create task in Agents Gateway
    # Update task status: created -> ready -> dispatched -> running
    # Transition through ready first if needed
    if task["status"] == "created":
        storage.update_task_status(task_id, "ready")
        task = storage.get_task(task_id)
    if task["status"] == "ready":
        storage.update_task_status(task_id, "dispatched")
        task = storage.get_task(task_id)

    try:
        gw_task = gateway.create_task(
            agent_id=agent_id,
            input_data=agent_input or task["brief"],
            idempotency_key=idem_key,
        )
        with storage.connect() as conn:
            conn.execute(
                "UPDATE agent_runs SET agents_gateway_task_id = ?, status = ? WHERE id = ?",
                (gw_task.id, "dispatched", agent_run["id"]),
            )
            conn.commit()
        agent_run = storage.get_agent_run(agent_run["id"])

        # Now run the task
        try:
            gw_task = gateway.run_task(gw_task.id)
            storage.update_agent_run_status(agent_run["id"], "running")
            # dispatched -> running is valid
            storage.update_task_status(task_id, "running")
            agent_run = storage.get_agent_run(agent_run["id"])
        except Exception as e:
            logger.warning("run_task_failed task_id=%s error=%s", task_id, e)
            agent_run = storage.update_agent_run_status(agent_run["id"], "failed")
            emit(storage, "dispatch.run_failed", f"Failed to run task: {e}",
                 objective_id=task["objective_id"], run_id=task["run_id"],
                 task_id=task_id, agent_run_id=agent_run["id"],
                 source="conductor")

    except Exception as e:
        logger.error("dispatch_failed task_id=%s error=%s", task_id, e)
        storage.update_agent_run_status(agent_run["id"], "failed")
        emit(storage, "dispatch.failed", f"Dispatch failed: {e}",
             objective_id=task["objective_id"], run_id=task["run_id"],
             task_id=task_id, agent_run_id=agent_run["id"],
             source="conductor")
        raise DispatchError(f"Failed to dispatch task: {e}")

    emit(storage, "dispatch.completed", f"Dispatched task {task_id} attempt {attempt}",
         objective_id=task["objective_id"], run_id=task["run_id"],
         task_id=task_id, agent_run_id=agent_run["id"],
         source="conductor")

    return agent_run


def reconcile_task(
    storage: ConductorStorage,
    gateway: BaseAgentsGatewayClient,
    agent_run_id: str,
) -> dict | None:
    agent_run = storage.get_agent_run(agent_run_id)
    if not agent_run:
        return None

    if not agent_run["agents_gateway_task_id"]:
        return agent_run

    try:
        gw_task = gateway.get_task(agent_run["agents_gateway_task_id"])
        gw_status = gw_task.status

        status_map = {
            "created": "dispatched",
            "queued": "queued",
            "running": "running",
            "waiting": "running",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        target = status_map.get(gw_status, "lost")

        if target != agent_run["status"]:
            updated = storage.update_agent_run_status(agent_run_id, target)

            # Also update associated task if in terminal state
            if target in {"completed", "failed", "cancelled"}:
                task = storage.get_task(agent_run["task_id"])
                if task and task["status"] not in {"completed", "failed", "cancelled"}:
                    # transition through dispatched/running if needed
                    if task["status"] == "created" or task["status"] == "ready":
                        storage.update_task_status(agent_run["task_id"], "dispatched")
                    if task["status"] == "dispatched":
                        storage.update_task_status(agent_run["task_id"], "running")
                    storage.update_task_status(agent_run["task_id"], target)

            # Store result summary if completed
            if target == "completed":
                with storage.connect() as conn:
                    conn.execute(
                        "UPDATE agent_runs SET result_summary = ? WHERE id = ?",
                        (gw_task.output[:200] if gw_task.output else "", agent_run_id),
                    )
                    conn.commit()

            emit(storage, "reconciliation.status_update",
                 f"Reconciled {agent_run_id} from {agent_run['status']} to {target}",
                 objective_id=agent_run.get("objective_id"), run_id=agent_run.get("run_id"),
                 task_id=agent_run["task_id"], agent_run_id=agent_run_id,
                 source="conductor")

        agent_run = storage.get_agent_run(agent_run_id)
        return agent_run

    except Exception as e:
        logger.warning("reconcile_error agent_run_id=%s error=%s", agent_run_id, e)
        storage.update_agent_run_status(agent_run_id, "lost")
        return storage.get_agent_run(agent_run_id)