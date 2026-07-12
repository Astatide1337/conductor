"""Composer events — thin wrappers over conductor.events.emit."""

from __future__ import annotations

from conductor.events import EventRecord, emit, list_events

__all__ = [
    "COMPOSER_EVENTS",
    "composer_emit",
    "composer_list_events",
]

# Canonical composer event types
COMPOSER_EVENTS = frozenset({
    "composer.objective_received",
    "composer.spec_normalizing",
    "composer.spec_normalized",
    "composer.context_built",
    "composer.plan_generated",
    "composer.plan_validation_failed",
    "composer.plan_repaired",
    "composer.plan_validated",
    "composer.plan_activated",
    "composer.task_ready",
    "composer.task_dispatching",
    "composer.task_dispatched",
    "composer.task_running",
    "composer.task_waiting_for_reply",
    "composer.interaction_received",
    "composer.interaction_answered",
    "composer.task_restarted",
    "composer.task_verifying",
    "composer.task_completed",
    "composer.task_blocked_external",
    "composer.integration_ready",
    "composer.integration_dispatched",
    "composer.integration_completed",
    "composer.final_verification_started",
    "composer.final_verification_passed",
    "composer.report_generated",
    "composer.objective_completed",
    "composer.objective_blocked_external",
    "composer.objective_failed",
})


def composer_emit(
    storage,
    event_type: str,
    message: str = "",
    *,
    objective_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    agent_run_id: str | None = None,
    payload: dict | None = None,
) -> EventRecord:
    """Emit a Composer event.  Never includes secrets in payload."""
    return emit(
        storage,
        event_type,
        message,
        objective_id=objective_id,
        run_id=run_id,
        task_id=task_id,
        agent_run_id=agent_run_id,
        payload=payload,
        source="composer",
    )


def composer_list_events(
    storage,
    objective_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[EventRecord]:
    return list_events(storage, objective_id=objective_id, limit=limit, offset=offset)
