"""Task dispatch coordination — sends work to Agents Gateway with idempotency protection."""

from datetime import UTC, datetime
from typing import Optional

from conductor.circuit import BreakerEvaluator
from conductor.clients.agents_gateway import (
    BaseAgentsGatewayClient,
    TaskInfo,
)
from conductor.clients.skills_gateway import BaseSkillsGatewayClient
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
    skills_client: BaseSkillsGatewayClient | None = None,
    registry=None,
    metrics=None,
) -> dict:
    task = storage.get_task(task_id)
    if not task:
        raise DispatchError(f"Task {task_id} not found")

    # ── Skills validation gate — runs BEFORE any state transition. ───────────
    # Required skills are validated against the Skills Gateway; if any are
    # missing, we refuse to dispatch entirely and leave the task in its
    # original state (no spurious created→ready→dispatched→running→blocked).
    # No agent_run row is created, no Agents Gateway call is made.
    if skills_client is not None:
        from conductor.clients.skills_gateway import validate_required_skills
        required = task.get("required_skills") or []
        valid, _, missing = validate_required_skills(skills_client, required)
        if missing:
            logger.warning(
                "dispatch_skills_missing task_id=%s missing=%s original_status=%s",
                task_id, missing, task["status"],
            )
            emit(
                storage, "task.skills_validation_failed",
                f"Task {task_id} missing required skills: {missing}",
                objective_id=task["objective_id"], run_id=task["run_id"],
                task_id=task_id, source="conductor",
                payload={"missing_skills": missing, "validated": valid,
                         "original_status": task["status"]},
            )
            if metrics:
                metrics.inc("conductor_dispatch_errors_total")
            # Task remains in original state (created or ready). Return structured
            # error without creating an agent_run or calling the gateway.
            return {
                "task_id": task_id,
                "status": task["status"],
                "agent_run": None,
                "error": f"missing required skills: {missing}",
                "missing_skills": missing,
                "validated_skills": valid,
            }
        else:
            emit(
                storage, "task.skills_validated",
                f"Task {task_id} required skills validated: {valid}",
                objective_id=task["objective_id"], run_id=task["run_id"],
                task_id=task_id, source="conductor",
                payload={"validated_skills": valid},
            )

    # ── Capability validation gate — runs AFTER skills, BEFORE transitions. ──
    # If a gateway registry is wired in, restore required_capabilities from task
    # metadata and validate each capability has at least one configured+enabled
    # provider. Missing capabilities block dispatch exactly like missing skills
    # — no agent_run row, no gateway call, task stays in its original state.
    # Degrade (capability exists but provider unhealthy) is reported but does
    # NOT block by default unless `require_healthy=True` is passed via registry
    # metadata — `registry.metadata` may carry {"require_healthy": True}.
    if registry is not None:
        from conductor.gateways.validation import (
            validate_required_capabilities,
            get_required_capabilities_from_task,
        )
        required_caps = get_required_capabilities_from_task(task)
        if required_caps:
            require_healthy = bool(getattr(registry, "metadata", {}) \
                .get("require_healthy", False) if hasattr(registry, "metadata") else False)
            result = validate_required_capabilities(
                registry, required_caps,
                require_healthy=require_healthy,
            )
            block_caps = result.missing if not require_healthy else (
                result.missing + result.degraded
            )
            if block_caps:
                logger.warning(
                    "dispatch_capabilities_missing task_id=%s missing=%s degraded=%s",
                    task_id, result.missing, result.degraded,
                )
                emit(
                    storage, "task.capabilities_validation_failed",
                    f"Task {task_id} missing required capabilities: {block_caps}",
                    objective_id=task["objective_id"], run_id=task["run_id"],
                    task_id=task_id, source="conductor",
                    payload={
                        "missing_capabilities": result.missing,
                        "degraded_capabilities": result.degraded,
                        "satisfied_capabilities": result.satisfied,
                        "original_status": task["status"],
                    },
                )
                if metrics:
                    metrics.inc("conductor_dispatch_errors_total")
                    metrics.inc("conductor_capability_validation_failed_total")
                return {
                    "task_id": task_id,
                    "status": task["status"],
                    "agent_run": None,
                    "error": f"missing required capabilities: {block_caps}",
                    "missing_capabilities": result.missing,
                    "degraded_capabilities": result.degraded,
                    "satisfied_capabilities": result.satisfied,
                }
            emit(
                storage, "task.capabilities_validated",
                f"Task {task_id} required capabilities validated: {result.satisfied}",
                objective_id=task["objective_id"], run_id=task["run_id"],
                task_id=task_id, source="conductor",
                payload={
                    "satisfied_capabilities": result.satisfied,
                    "degraded_capabilities": result.degraded,
                },
            )
            if metrics:
                metrics.inc("conductor_capability_validation_total")

    # ── Dispatch requested — fires after the gate passes (or no skills_client). ──
    emit(
        storage, "task.dispatch_requested",
        f"Dispatch requested for task {task_id} attempt {attempt}",
        objective_id=task["objective_id"], run_id=task["run_id"],
        task_id=task_id, source="conductor",
        payload={"attempt": attempt, "agent_id": agent_id},
    )

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
    if metrics:
        metrics.inc("conductor_agent_runs_total")

    # Update task status: created -> ready -> dispatched -> running
    # Transition through ready first if needed
    if task["status"] == "created":
        storage.update_task_status(task_id, "ready")
        task = storage.get_task(task_id)
    if task["status"] == "ready":
        storage.update_task_status(task_id, "dispatched")
        task = storage.get_task(task_id)

    # Emit agent_run.created now that the row exists and was linked to its task.
    emit(
        storage, "agent_run.created",
        f"Agent run {agent_run['id']} created for task {task_id}",
        objective_id=task["objective_id"], run_id=task["run_id"],
        task_id=task_id, agent_run_id=agent_run["id"], source="conductor",
    )

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

        # Emit task.dispatched now that the gateway task is real.
        emit(
            storage, "task.dispatched",
            f"Task {task_id} dispatched to gateway task {gw_task.id}",
            objective_id=task["objective_id"], run_id=task["run_id"],
            task_id=task_id, agent_run_id=agent_run["id"], source="conductor",
            payload={"agents_gateway_task_id": gw_task.id},
        )
        # Also emit the spec gateway.* audit event for the cockpit timeline.
        emit(
            storage, "gateway.agents.dispatch",
            f"Dispatched task {task_id} to agents gateway task {gw_task.id}",
            objective_id=task["objective_id"], run_id=task["run_id"],
            task_id=task_id, source="conductor",
            payload={
                "gateway_id": "agents", "gateway_kind": "agents",
                "agent_id": agent_id,
                "agents_gateway_task_id": gw_task.id,
            },
        )

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
            emit(storage, "agent_run.failed", f"Agent run failed: {e}",
                 objective_id=task["objective_id"], run_id=task["run_id"],
                 task_id=task_id, agent_run_id=agent_run["id"],
                 source="conductor")
            emit(storage, "dispatch.run_failed", f"Failed to run task: {e}",
                 objective_id=task["objective_id"], run_id=task["run_id"],
                 task_id=task_id, agent_run_id=agent_run["id"],
                 source="conductor")

    except Exception as e:
        logger.error("dispatch_failed task_id=%s error=%s", task_id, e)
        storage.update_agent_run_status(agent_run["id"], "failed")
        emit(storage, "agent_run.failed", f"Agent run failed: {e}",
             objective_id=task["objective_id"], run_id=task["run_id"],
             task_id=task_id, agent_run_id=agent_run["id"],
             source="conductor")
        emit(storage, "dispatch.failed", f"Dispatch failed: {e}",
             objective_id=task["objective_id"], run_id=task["run_id"],
             task_id=task_id, agent_run_id=agent_run["id"],
             source="conductor")
        if metrics:
            metrics.inc("conductor_dispatch_errors_total")
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
    metrics=None,
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

            # Fan out: emit both the detailed `agent_run.<state>` lifecycle
            # event (consumed by the audit/metrics pipeline) and the
            # `reconciliation.status_update` event (consumed by operators).
            emit(storage, f"agent_run.{target}",
                 f"Agent run {agent_run_id} → {target}",
                 objective_id=agent_run.get("objective_id"), run_id=agent_run.get("run_id"),
                 task_id=agent_run["task_id"], agent_run_id=agent_run_id,
                 source="conductor",
                 payload={"from": agent_run["status"], "to": target})
            if target in {"completed", "failed", "cancelled"}:
                # Spec names: agent_run.completed/failed both already covered above.
                pass
            emit(storage, "agent_run.reconciled",
                 f"Reconciled {agent_run_id} ({agent_run['status']} → {target})",
                 objective_id=agent_run.get("objective_id"), run_id=agent_run.get("run_id"),
                 task_id=agent_run["task_id"], agent_run_id=agent_run_id,
                 source="conductor",
                 payload={"from": agent_run["status"], "to": target})
            emit(storage, "reconciliation.status_update",
                 f"Reconciled {agent_run_id} from {agent_run['status']} to {target}",
                 objective_id=agent_run.get("objective_id"), run_id=agent_run.get("run_id"),
                 task_id=agent_run["task_id"], agent_run_id=agent_run_id,
                 source="conductor")

        # Always ingest artifacts on reconcile — gateway may have produced them
        # without yet flipping the task to "completed". Idempotent: writing the
        # same artifact_refs set is harmless.
        try:
            artifacts = gateway.get_artifacts(agent_run["agents_gateway_task_id"])
        except Exception as e:
            logger.warning("reconcile_artifacts_fetch_failed agent_run_id=%s error=%s", agent_run_id, e)
            artifacts = []

        if artifacts:
            existing_refs = agent_run.get("artifact_refs") or []
            existing_ids = {a.get("id") for a in existing_refs}
            new_refs = [
                {"id": a.id, "name": a.name, "path": a.path, "size_bytes": a.size_bytes}
                for a in artifacts if a.id not in existing_ids
            ]
            merged = list(existing_refs) + new_refs
            if new_refs:
                storage.set_agent_run_artifacts(agent_run_id, merged)
                if metrics:
                    metrics.inc("conductor_artifacts_ingested_total", amount=float(len(new_refs)))
                emit(storage, "artifacts.ingested",
                     f"Ingested {len(new_refs)} artifact(s) for {agent_run_id}",
                     objective_id=agent_run.get("objective_id"), run_id=agent_run.get("run_id"),
                     task_id=agent_run["task_id"], agent_run_id=agent_run_id,
                     source="conductor",
                     payload={"count": len(new_refs),
                              "names": [r.get("name") for r in new_refs]})
                emit(storage, "reconciliation.artifacts_ingested",
                     f"Ingested {len(new_refs)} artifact(s) for {agent_run_id}",
                     objective_id=agent_run.get("objective_id"), run_id=agent_run.get("run_id"),
                     task_id=agent_run["task_id"], agent_run_id=agent_run_id,
                     source="conductor")

        agent_run = storage.get_agent_run(agent_run_id)
        return agent_run

    except Exception as e:
        logger.warning("reconcile_error agent_run_id=%s error=%s", agent_run_id, e)
        storage.update_agent_run_status(agent_run_id, "lost")
        if metrics:
            metrics.inc("conductor_reconciliation_errors_total")
        emit(storage, "agent_run.failed",
             f"Reconcile failed for {agent_run_id}: {e}",
             objective_id=agent_run.get("objective_id"), run_id=agent_run.get("run_id"),
             task_id=agent_run["task_id"], agent_run_id=agent_run_id,
             source="conductor")
        return storage.get_agent_run(agent_run_id)


def reconcile_all(
    storage: ConductorStorage,
    gateway: BaseAgentsGatewayClient,
    statuses: tuple[str, ...] | None = None,
    metrics=None,
) -> dict:
    """Reconcile all in-flight agent_runs against the gateway. Called after restart.

    Returns a summary: {reconciled: int, transitions: int, errors: int, by_target: {status: count}}.
    Safe to call repeatedly — only agent_runs whose status actually changes record transitions.
    """
    candidates = storage.list_inflight_agent_runs(statuses=statuses)
    by_target: dict[str, int] = {}
    transitions = 0
    errors = 0
    reconciled = 0

    for ar in candidates:
        ar_id = ar["id"]
        before_status = ar["status"]
        try:
            after = reconcile_task(storage, gateway, ar_id, metrics=metrics)
            reconciled += 1
            if after and after.get("status") != before_status:
                transitions += 1
                by_target[after["status"]] = by_target.get(after["status"], 0) + 1
        except Exception as e:
            logger.warning("reconcile_all_error agent_run_id=%s error=%s", ar_id, e)
            errors += 1
            if metrics:
                metrics.inc("conductor_reconciliation_errors_total")

    summary = {
        "reconciled": reconciled,
        "transitions": transitions,
        "errors": errors,
        "by_target": by_target,
        "candidate_count": len(candidates),
    }
    emit(storage, "reconciliation.batch", f"Reconciled {reconciled} agent runs (transitions={transitions}, errors={errors})", source="conductor")
    return summary