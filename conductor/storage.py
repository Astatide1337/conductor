"""SQLite storage layer with durable state. Implements the full Conductor data model.

Tables: objectives, objective_runs, tasks, agent_runs, approvals,
        events, planner_turns, cost_ledger
"""

import os
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Optional

from conductor.logging import get_logger

logger = get_logger()

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