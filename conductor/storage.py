"""SQLite storage layer with durable state. Implements the full Conductor data model.

Tables: objectives, objective_runs, tasks, agent_runs, approvals,
        events, planner_turns, cost_ledger

State machine transitions enforced on every status mutation.
"""

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Optional

from conductor.logging import get_logger

logger = get_logger()

# ── State machine transition maps ──────────────────────────────────────────

OBJECTIVE_TRANSITIONS: dict[str, set[str]] = {
    "created": {"active"},
    "active": {"paused", "blocked", "completed", "failed", "cancelled"},
    "paused": {"active"},
    "blocked": {"active", "failed"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}

OBJECTIVE_TERMINAL = {"completed", "failed", "cancelled"}

TASK_TRANSITIONS: dict[str, set[str]] = {
    "created": {"ready"},
    "ready": {"dispatched", "cancelled"},
    "dispatched": {"running", "cancelled"},
    "running": {"completed", "failed", "blocked", "cancelled"},
    "blocked": {"ready", "running"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}

TASK_TERMINAL = {"completed", "failed", "cancelled"}

AGENT_RUN_TRANSITIONS: dict[str, set[str]] = {
    "created": {"dispatched"},
    "dispatched": {"queued", "running", "failed", "cancelled"},
    "queued": {"running", "failed", "cancelled"},
    "running": {"completed", "failed", "cancelled", "lost"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "lost": {"running", "failed"},  # can recover from lost
}

APPROVAL_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"approved", "rejected", "expired", "cancelled"},
    "approved": set(),
    "rejected": set(),
    "expired": set(),
    "cancelled": set(),
}


class TransitionError(ValueError):
    """Raised when an invalid state transition is attempted."""


def validate_transition(transitions: dict, current: str, target: str) -> bool:
    valid = transitions.get(current, set())
    if target not in valid:
        raise TransitionError(
            f"Invalid transition from '{current}' to '{target}'. "
            f"Valid targets: {sorted(valid) if valid else 'none (terminal)'}"
        )
    return True

SCHEMA = """
CREATE TABLE IF NOT EXISTS objectives (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created',
    priority TEXT NOT NULL DEFAULT 'normal',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS objective_runs (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    started_at TEXT,
    finished_at TEXT,
    planner_mode TEXT NOT NULL DEFAULT 'manual',
    max_iterations INTEGER NOT NULL DEFAULT 50,
    max_cost_usd REAL NOT NULL DEFAULT 10.0,
    max_concurrent_tasks INTEGER NOT NULL DEFAULT 4,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (objective_id) REFERENCES objectives(id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    title TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created',
    task_type TEXT NOT NULL DEFAULT 'ship',
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    required_skills_json TEXT NOT NULL DEFAULT '[]',
    dispatch_profile TEXT NOT NULL DEFAULT '',
    approval_required INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (objective_id) REFERENCES objectives(id),
    FOREIGN KEY (run_id) REFERENCES objective_runs(id)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agents_gateway_task_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'created',
    idempotency_key TEXT NOT NULL DEFAULT '',
    dispatch_profile TEXT NOT NULL DEFAULT '',
    runtime_type TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    last_reconciled_at TEXT,
    result_summary TEXT NOT NULL DEFAULT '',
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    action_type TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (objective_id) REFERENCES objectives(id),
    FOREIGN KEY (run_id) REFERENCES objective_runs(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    objective_id TEXT,
    run_id TEXT,
    task_id TEXT,
    agent_run_id TEXT,
    event_type TEXT NOT NULL DEFAULT 'system',
    message TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS planner_turns (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    input_summary TEXT NOT NULL DEFAULT '',
    output_json TEXT NOT NULL DEFAULT '{}',
    valid INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (objective_id) REFERENCES objectives(id),
    FOREIGN KEY (run_id) REFERENCES objective_runs(id)
);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'system',
    amount_usd REAL NOT NULL DEFAULT 0.0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (objective_id) REFERENCES objectives(id),
    FOREIGN KEY (run_id) REFERENCES objective_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_objective_runs_obj ON objective_runs(objective_id);
CREATE INDEX IF NOT EXISTS idx_tasks_obj ON tasks(objective_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_approvals_obj ON approvals(objective_id);
CREATE INDEX IF NOT EXISTS idx_events_obj ON events(objective_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_planner_turns_run ON planner_turns(run_id);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_run ON cost_ledger(run_id);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _uid() -> str:
    return str(uuid.uuid4())


class ConductorStorage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        self._initialized = True
        logger.info("storage_initialized path=%s", self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def connect(self) -> sqlite3.Connection:
        return self._connect()

    # ── Objectives ──────────────────────────────────────────────────────

    def create_objective(
        self,
        title: str,
        description: str = "",
        priority: str = "normal",
        created_by: str = "",
        metadata: dict | None = None,
    ) -> dict:
        now = _now_iso()
        obj_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO objectives (id, title, description, status, priority,
                   created_at, updated_at, created_by, metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    obj_id, title, description, "created", priority,
                    now, now, created_by, json.dumps(metadata or {}),
                ),
            )
            conn.commit()
        return self.get_objective(obj_id)

    def get_objective(self, objective_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM objectives WHERE id = ?", (objective_id,)
            ).fetchone()
        return self._row_to_objective(row) if row else None

    def list_objectives(
        self, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        params: list = []
        clause = "1=1"
        if status:
            clause = "status = ?"
            params.append(status)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM objectives WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return [self._row_to_objective(r) for r in rows]

    def update_objective_status(self, objective_id: str, target: str) -> dict | None:
        current = self.get_objective(objective_id)
        if not current:
            return None
        validate_transition(OBJECTIVE_TRANSITIONS, current["status"], target)
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE objectives SET status = ?, updated_at = ? WHERE id = ?",
                (target, now, objective_id),
            )
            conn.commit()
        return self.get_objective(objective_id)

    # ── Objective Runs ───────────────────────────────────────────────────

    def create_run(
        self,
        objective_id: str,
        planner_mode: str = "manual",
        max_iterations: int = 50,
        max_cost_usd: float = 10.0,
        max_concurrent_tasks: int = 4,
        metadata: dict | None = None,
    ) -> dict | None:
        objective = self.get_objective(objective_id)
        if not objective:
            return None
        run_id = _uid()
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO objective_runs (id, objective_id, status, started_at,
                   planner_mode, max_iterations, max_cost_usd, max_concurrent_tasks,
                   metadata_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, objective_id, "created", now,
                    planner_mode, max_iterations, max_cost_usd, max_concurrent_tasks,
                    json.dumps(metadata or {}),
                ),
            )
            conn.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM objective_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, objective_id: str, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM objective_runs WHERE objective_id = ? ORDER BY started_at DESC LIMIT ?",
                (objective_id, limit),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def update_run_status(self, run_id: str, target: str) -> dict | None:
        current = self.get_run(run_id)
        if not current:
            return None
        cs = current["status"]
        if cs == "created" and target not in OBJECTIVE_TRANSITIONS["created"]:
            if "active" in OBJECTIVE_TRANSITIONS["created"]:
                self.update_run_status(run_id, "active")
                cs = "active"
        validate_transition(OBJECTIVE_TRANSITIONS, cs, target)
        now = _now_iso()
        if target in OBJECTIVE_TERMINAL:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE objective_runs SET status = ?, finished_at = ? WHERE id = ?",
                    (target, now, run_id),
                )
                conn.commit()
        else:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE objective_runs SET status = ? WHERE id = ?",
                    (target, run_id),
                )
                conn.commit()
        return self.get_run(run_id)

    # ── Tasks ────────────────────────────────────────────────────────────

    def create_task(
        self,
        objective_id: str,
        run_id: str,
        title: str,
        brief: str = "",
        task_type: str = "ship",
        depends_on: list[str] | None = None,
        required_skills: list[str] | None = None,
        dispatch_profile: str = "",
        approval_required: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        now = _now_iso()
        task_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tasks (id, objective_id, run_id, title, brief, status,
                   task_type, depends_on_json, required_skills_json, dispatch_profile,
                   approval_required, created_at, updated_at, metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, objective_id, run_id, title, brief, "created",
                    task_type, json.dumps(depends_on or []), json.dumps(required_skills or []),
                    dispatch_profile, int(approval_required), now, now, json.dumps(metadata or {}),
                ),
            )
            conn.commit()
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(
        self,
        objective_id: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses = ["1=1"]
        params: list = []
        if objective_id:
            clauses.append("objective_id = ?")
            params.append(objective_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE {where} ORDER BY created_at ASC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def update_task_status(self, task_id: str, target: str) -> dict | None:
        current = self.get_task(task_id)
        if not current:
            return None
        validate_transition(TASK_TRANSITIONS, current["status"], target)
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (target, now, task_id),
            )
            conn.commit()
        return self.get_task(task_id)

    # ── Agent Runs ───────────────────────────────────────────────────────

    def create_agent_run(
        self,
        task_id: str,
        idempotency_key: str = "",
        dispatch_profile: str = "",
        runtime_type: str = "",
        metadata: dict | None = None,
    ) -> dict:
        now = _now_iso()
        ar_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO agent_runs (id, task_id, agents_gateway_task_id,
                   attempt, status, idempotency_key, dispatch_profile, runtime_type,
                   started_at, result_summary, artifact_refs_json, metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ar_id, task_id, None, 1, "created",
                    idempotency_key, dispatch_profile, runtime_type,
                    now, "", json.dumps([]), json.dumps(metadata or {}),
                ),
            )
            conn.commit()
        return self.get_agent_run(ar_id)

    def get_agent_run(self, agent_run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (agent_run_id,)
            ).fetchone()
        return self._row_to_agent_run(row) if row else None

    def get_agent_run_by_idempotency(self, idempotency_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE idempotency_key = ? ORDER BY attempt DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
        return self._row_to_agent_run(row) if row else None

    def update_agent_run_status(self, agent_run_id: str, target: str) -> dict | None:
        current = self.get_agent_run(agent_run_id)
        if not current:
            return None
        validate_transition(AGENT_RUN_TRANSITIONS, current["status"], target)
        now = _now_iso()
        updates = "status = ?, last_reconciled_at = ?"
        params: list = [target, now, agent_run_id]
        if target in {"completed", "failed", "cancelled"}:
            updates += ", finished_at = ?"
            params.insert(2, now)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE agent_runs SET {updates} WHERE id = ?",
                params,
            )
            conn.commit()
        return self.get_agent_run(agent_run_id)

    # ── Approvals ────────────────────────────────────────────────────────

    def create_approval(
        self,
        objective_id: str,
        run_id: str,
        action_type: str,
        description: str = "",
        risk_level: str = "medium",
        task_id: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        now = _now_iso()
        approval_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO approvals (id, objective_id, run_id, task_id,
                   action_type, description, risk_level, status, requested_at,
                   payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    approval_id, objective_id, run_id, task_id,
                    action_type, description, risk_level, "pending", now,
                    json.dumps(payload or {}),
                ),
            )
            conn.commit()
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return self._row_to_approval(row) if row else None

    def list_approvals(
        self,
        objective_id: str | None = None,
        run_id: str | None = None,
        status: str | None = "pending",
        limit: int = 50,
    ) -> list[dict]:
        clauses = ["1=1"]
        params: list = []
        if objective_id:
            clauses.append("objective_id = ?")
            params.append(objective_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM approvals WHERE {where} ORDER BY requested_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [self._row_to_approval(r) for r in rows]

    def update_approval_status(
        self,
        approval_id: str,
        target: str,
        decided_by: str = "",
        decision_reason: str = "",
    ) -> dict | None:
        current = self.get_approval(approval_id)
        if not current:
            return None
        validate_transition(APPROVAL_TRANSITIONS, current["status"], target)
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, decision_reason = ? WHERE id = ?",
                (target, now, decided_by, decision_reason, approval_id),
            )
            conn.commit()
        return self.get_approval(approval_id)

    # ── Cost ledger ──────────────────────────────────────────────────────

    def record_cost(
        self,
        objective_id: str,
        run_id: str,
        source: str = "planner",
        amount_usd: float = 0.0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        description: str = "",
    ) -> dict:
        now = _now_iso()
        cost_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO cost_ledger (id, objective_id, run_id, source,
                   amount_usd, tokens_in, tokens_out, description, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (cost_id, objective_id, run_id, source, amount_usd, tokens_in, tokens_out, description, now),
            )
            conn.commit()
        return {
            "id": cost_id, "objective_id": objective_id, "run_id": run_id,
            "source": source, "amount_usd": amount_usd, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "description": description, "created_at": now,
        }

    def total_cost_for_run(self, run_id: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) FROM cost_ledger WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return float(row[0])

    # ── Planner turns ────────────────────────────────────────────────────

    def record_planner_turn(
        self,
        objective_id: str,
        run_id: str,
        input_summary: str = "",
        output: dict | None = None,
        valid: bool = True,
        error: str = "",
        model: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ) -> dict:
        now = _now_iso()
        turn_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO planner_turns (id, objective_id, run_id,
                   input_summary, output_json, valid, error, model,
                   tokens_in, tokens_out, cost_usd, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (turn_id, objective_id, run_id, input_summary,
                 json.dumps(output or {}), int(valid), error, model,
                 tokens_in, tokens_out, cost_usd, now),
            )
            conn.commit()
        return {
            "id": turn_id, "objective_id": objective_id, "run_id": run_id,
            "input_summary": input_summary, "output": output or {},
            "valid": valid, "error": error, "model": model,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_usd": cost_usd, "created_at": now,
        }

    def count_planner_turns_for_run(self, run_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM planner_turns WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0])

    def count_agent_runs_for_run(self, run_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM agent_runs ar
                   JOIN tasks t ON ar.task_id = t.id
                   WHERE t.run_id = ?""",
                (run_id,),
            ).fetchone()
        return int(row[0])

    # ── Row helpers ──────────────────────────────────────────────────────

    def _row_to_objective(self, row) -> dict:
        return {
            "id": row["id"], "title": row["title"], "description": row["description"],
            "status": row["status"], "priority": row["priority"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "created_by": row["created_by"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        }

    def _row_to_run(self, row) -> dict:
        return {
            "id": row["id"], "objective_id": row["objective_id"],
            "status": row["status"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "planner_mode": row["planner_mode"],
            "max_iterations": row["max_iterations"], "max_cost_usd": row["max_cost_usd"],
            "max_concurrent_tasks": row["max_concurrent_tasks"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        }

    def _row_to_task(self, row) -> dict:
        return {
            "id": row["id"], "objective_id": row["objective_id"], "run_id": row["run_id"],
            "title": row["title"], "brief": row["brief"],
            "status": row["status"], "task_type": row["task_type"],
            "depends_on": json.loads(row["depends_on_json"]) if row["depends_on_json"] else [],
            "required_skills": json.loads(row["required_skills_json"]) if row["required_skills_json"] else [],
            "dispatch_profile": row["dispatch_profile"],
            "approval_required": bool(row["approval_required"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        }

    def _row_to_agent_run(self, row) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"], "task_id": row["task_id"],
            "agents_gateway_task_id": row["agents_gateway_task_id"],
            "attempt": row["attempt"], "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "dispatch_profile": row["dispatch_profile"],
            "runtime_type": row["runtime_type"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "last_reconciled_at": row["last_reconciled_at"],
            "result_summary": row["result_summary"],
            "artifact_refs": json.loads(row["artifact_refs_json"]) if row["artifact_refs_json"] else [],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        }

    def _row_to_approval(self, row) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"], "objective_id": row["objective_id"], "run_id": row["run_id"],
            "task_id": row["task_id"],
            "action_type": row["action_type"], "description": row["description"],
            "risk_level": row["risk_level"], "status": row["status"],
            "requested_at": row["requested_at"], "decided_at": row["decided_at"],
            "decided_by": row["decided_by"], "decision_reason": row["decision_reason"],
            "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
        }