"""Append-only event audit trail.

Provides emit() helpers that write to the events table and structured logs.
"""

import json
from typing import Optional

from pydantic import BaseModel

from conductor.storage import ConductorStorage, _uid, _now_iso


class EventRecord(BaseModel):
    id: str
    objective_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    agent_run_id: str | None = None
    event_type: str = "system"
    message: str = ""
    payload: dict | None = None
    created_at: str
    source: str = "system"


EVENT_SOURCES = {
    "user",
    "conductor",
    "planner",
    "agents_gateway",
    "skills_gateway",
    "mcp_gateway",
    "wiki_mcp",
    "system",
}


def emit(
    storage: ConductorStorage,
    event_type: str,
    message: str = "",
    objective_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    agent_run_id: str | None = None,
    payload: dict | None = None,
    source: str = "conductor",
) -> EventRecord:
    now = _now_iso()
    record = EventRecord(
        id=_uid(),
        objective_id=objective_id,
        run_id=run_id,
        task_id=task_id,
        agent_run_id=agent_run_id,
        event_type=event_type,
        message=message,
        payload=payload or {},
        created_at=now,
        source=source,
    )
    with storage.connect() as conn:
        conn.execute(
            """INSERT INTO events (id, objective_id, run_id, task_id, agent_run_id,
               event_type, message, payload_json, created_at, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                record.id,
                record.objective_id,
                record.run_id,
                record.task_id,
                record.agent_run_id,
                record.event_type,
                record.message,
                json.dumps(record.payload),
                record.created_at,
                record.source,
            ),
        )
        conn.commit()
    return record


def list_events(
    storage: ConductorStorage,
    objective_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[EventRecord]:
    clauses: list[str] = []
    params: list = []
    if objective_id:
        clauses.append("objective_id = ?")
        params.append(objective_id)
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)

    where = " AND ".join(clauses) if clauses else "1=1"
    with storage.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def _row_to_event(row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        objective_id=row["objective_id"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        agent_run_id=row["agent_run_id"],
        event_type=row["event_type"],
        message=row["message"],
        payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
        created_at=row["created_at"],
        source=row["source"],
    )