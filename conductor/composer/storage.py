"""Composition state storage — SQLite tables for Composer.

Uses the same connection-per-method pattern as ``conductor.storage``.
Adds five composer-specific tables alongside the existing Conductor
tables via ``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from conductor.composer.models import (
    ComposerContext,
    ComposerPlan,
    ComposerReport,
    ComposerSpec,
    InteractionDecision,
    NormalizedSpec,
    SpecRepository,
    TaskNode,
    VerificationSpec,
)

__all__ = ["COMPOSER_SCHEMA", "ComposerStorage"]


COMPOSER_SCHEMA = """
CREATE TABLE IF NOT EXISTS composer_specs (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    title TEXT NOT NULL,
    raw_spec TEXT NOT NULL DEFAULT '',
    normalized_spec_json TEXT NOT NULL DEFAULT '{}',
    repository_url TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT 'master',
    status TEXT NOT NULL DEFAULT 'received',
    previous_status TEXT,
    paused_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (objective_id) REFERENCES objectives(id)
);

CREATE TABLE IF NOT EXISTS composer_plans (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    spec_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    plan_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    activated_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (objective_id) REFERENCES objectives(id),
    FOREIGN KEY (spec_id) REFERENCES composer_specs(id)
);

CREATE TABLE IF NOT EXISTS composer_plan_tasks (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    conductor_task_id TEXT,
    agents_gateway_task_id TEXT,
    task_type TEXT NOT NULL DEFAULT 'implementation',
    title TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    ownership_notes TEXT NOT NULL DEFAULT '',
    dependencies_json TEXT NOT NULL DEFAULT '[]',
    file_scope_json TEXT NOT NULL DEFAULT '[]',
    harness_profile TEXT NOT NULL DEFAULT 'opencode-deepseek',
    required_skills_json TEXT NOT NULL DEFAULT '[]',
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    verification_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    branch TEXT,
    commit_sha TEXT,
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    previous_status TEXT,
    paused_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES composer_plans(id)
);

CREATE TABLE IF NOT EXISTS composer_interaction_decisions (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    plan_task_id TEXT,
    agents_gateway_interaction_id TEXT,
    action TEXT NOT NULL DEFAULT 'reply',
    reply TEXT NOT NULL DEFAULT '',
    decision_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (objective_id) REFERENCES objectives(id)
);

CREATE TABLE IF NOT EXISTS composer_reports (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    html_artifact_ref TEXT NOT NULL DEFAULT '',
    json_artifact_ref TEXT NOT NULL DEFAULT '',
    final_branch TEXT NOT NULL DEFAULT '',
    final_commit_sha TEXT NOT NULL DEFAULT '',
    pr_url TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (objective_id) REFERENCES objectives(id)
);

CREATE INDEX IF NOT EXISTS idx_composer_specs_obj ON composer_specs(objective_id);
CREATE INDEX IF NOT EXISTS idx_composer_plans_obj ON composer_plans(objective_id);
CREATE INDEX IF NOT EXISTS idx_composer_plans_spec ON composer_plans(spec_id);
CREATE INDEX IF NOT EXISTS idx_composer_plan_tasks_plan ON composer_plan_tasks(plan_id);
CREATE INDEX IF NOT EXISTS idx_composer_interaction_obj ON composer_interaction_decisions(objective_id);
CREATE INDEX IF NOT EXISTS idx_composer_reports_obj ON composer_reports(objective_id);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _uid() -> str:
    import uuid

    return str(uuid.uuid4())


class ComposerStorage:
    """Durable storage for Composer state.

    Shares the same SQLite database as conductors core state.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(COMPOSER_SCHEMA)
            self._apply_migrations(conn)
            conn.commit()
        self._initialized = True

    # Columns added after initial schema release.  Each migration is
    # idempotent ( guarded by PRAGMA table_info ) so re-running against
    # an already-migrated database is a no-op.
    _MIGRATIONS: list[tuple[str, str, str]] = [
        ("composer_specs", "previous_status", "TEXT"),
        ("composer_specs", "paused_at", "TEXT"),
        ("composer_plan_tasks", "title", "TEXT NOT NULL DEFAULT ''"),
        ("composer_plan_tasks", "goal", "TEXT NOT NULL DEFAULT ''"),
        ("composer_plan_tasks", "ownership_notes", "TEXT NOT NULL DEFAULT ''"),
        ("composer_plan_tasks", "previous_status", "TEXT"),
        ("composer_plan_tasks", "paused_at", "TEXT"),
    ]

    def _apply_migrations(self, conn) -> None:
        existing: dict[str, set[str]] = {}
        for table, _col, _def in self._MIGRATIONS:
            if table not in existing:
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                existing[table] = {r[1] for r in rows}
            if _col not in existing[table]:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {_col} {_def}")
                existing[table].add(_col)

    def _connect(self):
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Specs ──────────────────────────────────────────────────────────

    def create_spec(
        self,
        objective_id: str,
        title: str,
        raw_spec: str = "",
        repository_url: str = "",
        base_branch: str = "master",
    ) -> dict:
        spec_id = f"spec_{_uid()}"
        now = _now_iso()
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """INSERT INTO composer_specs (id, objective_id, title, raw_spec,
                   normalized_spec_json, repository_url, base_branch, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (spec_id, objective_id, title, raw_spec, "{}", repository_url, base_branch, "received", now, now),
            )
            conn.commit()
        return self.get_spec(spec_id)  # type: ignore

    def get_spec(self, spec_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM composer_specs WHERE id = ?", (spec_id,)
            ).fetchone()
        return self._row_to_composer_spec(row) if row else None

    def get_spec_by_objective(self, objective_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM composer_specs WHERE objective_id = ? ORDER BY created_at DESC LIMIT 1",
                (objective_id,),
            ).fetchone()
        return self._row_to_composer_spec(row) if row else None

    def update_spec(
        self,
        spec_id: str,
        *,
        normalized_spec: dict | None = None,
        status: str | None = None,
        title: str | None = None,
        repository_url: str | None = None,
        base_branch: str | None = None,
        previous_status: str | None = None,
        paused_at: str | None = None,
    ) -> dict | None:
        now = _now_iso()
        sets: list[str] = []
        params: list = []
        if normalized_spec is not None:
            sets.append("normalized_spec_json = ?")
            params.append(json.dumps(normalized_spec))
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if repository_url is not None:
            sets.append("repository_url = ?")
            params.append(repository_url)
        if base_branch is not None:
            sets.append("base_branch = ?")
            params.append(base_branch)
        if previous_status is not None:
            sets.append("previous_status = ?")
            params.append(previous_status)
        if paused_at is not None:
            sets.append("paused_at = ?")
            params.append(paused_at)
        params.append(now)
        params.append(spec_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE composer_specs SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                params,
            )
            conn.commit()
        return self.get_spec(spec_id)

    # ── Plans ──────────────────────────────────────────────────────────

    def create_plan(
        self,
        objective_id: str,
        spec_id: str,
        plan: ComposerPlan,
    ) -> dict:
        now = _now_iso()
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """INSERT INTO composer_plans (id, objective_id, spec_id, version,
                   status, plan_json, created_at, activated_at, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    plan.id,
                    objective_id,
                    spec_id,
                    plan.version,
                    plan.status,
                    plan.model_dump_json(),
                    now,
                    plan.activated_at,
                    plan.completed_at,
                ),
            )
            conn.commit()
            # Also persist task rows
            for task in plan.tasks:
                self._upsert_plan_task(conn, plan.id, task.model_dump())
            if plan.integration:
                self._upsert_plan_task(conn, plan.id, plan.integration.model_dump())
        return self.get_plan(plan.id)  # type: ignore

    def get_plan(self, plan_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM composer_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        return self._row_to_composer_plan(row) if row else None

    def get_plan_by_objective(self, objective_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM composer_plans WHERE objective_id = ? ORDER BY created_at DESC LIMIT 1",
                (objective_id,),
            ).fetchone()
        return self._row_to_composer_plan(row) if row else None

    def count_plans_by_objective(self, objective_id: str) -> int:
        """Number of plan rows for an objective.

        Used by the planning-idempotency proof test to assert exactly one
        plan row after a supervisor tick that crashed once before the
        spec.status update landed.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM composer_plans WHERE objective_id = ?",
                (objective_id,),
            ).fetchone()
            return int(row["n"]) if row else 0

    def update_plan(
        self,
        plan_id: str,
        *,
        status: str | None = None,
        activated_at: str | None = None,
        completed_at: str | None = None,
        plan: ComposerPlan | None = None,
    ) -> dict | None:
        sets: list[str] = []
        params: list = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if activated_at is not None:
            sets.append("activated_at = ?")
            params.append(activated_at)
        if completed_at is not None:
            sets.append("completed_at = ?")
            params.append(completed_at)
        if plan is not None:
            sets.append("plan_json = ?")
            params.append(plan.model_dump_json())
        if not sets:
            return self.get_plan(plan_id)
        params.append(plan_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE composer_plans SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            conn.commit()
        return self.get_plan(plan_id)

    # ── Plan Tasks ─────────────────────────────────────────────────────

    def _upsert_plan_task(self, conn, plan_id: str, task: dict) -> None:
        now = _now_iso()
        node_key = task.get("node_id", "")
        existing = conn.execute(
            "SELECT id FROM composer_plan_tasks WHERE plan_id = ? AND node_key = ?",
            (plan_id, node_key),
        ).fetchone()
        if existing:
            # Update existing task — preserve durable plan-node identity
            # fields (title, goal, ownership_notes) unless caller supplies
            # explicit overrides.
            conn.execute(
                """UPDATE composer_plan_tasks SET
                   conductor_task_id = ?, agents_gateway_task_id = ?,
                   task_type = ?, title = ?, goal = ?, ownership_notes = ?,
                   status = ?, branch = ?, commit_sha = ?,
                   artifact_refs_json = ?, metadata_json = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    task.get("conductor_task_id"),
                    task.get("agents_gateway_task_id"),
                    task.get("task_type", "implementation"),
                    task.get("title", ""),
                    task.get("goal", ""),
                    task.get("ownership_notes", ""),
                    task.get("status", "pending"),
                    task.get("branch"),
                    task.get("commit_sha"),
                    json.dumps(task.get("artifact_refs", [])),
                    json.dumps(task.get("metadata", {})),
                    now,
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """INSERT INTO composer_plan_tasks
                   (id, plan_id, node_key, conductor_task_id, agents_gateway_task_id,
                    task_type, title, goal, ownership_notes,
                    dependencies_json, file_scope_json, harness_profile,
                    required_skills_json, required_capabilities_json, verification_json,
                    status, branch, commit_sha, artifact_refs_json, metadata_json,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"ptask_{_uid()}", plan_id, node_key,
                    task.get("conductor_task_id"),
                    task.get("agents_gateway_task_id"),
                    task.get("task_type", "implementation"),
                    task.get("title", ""),
                    task.get("goal", ""),
                    task.get("ownership_notes", ""),
                    json.dumps(task.get("dependencies", [])),
                    json.dumps(task.get("file_scope", [])),
                    task.get("harness_profile", "opencode-deepseek"),
                    json.dumps(task.get("required_skills", [])),
                    json.dumps(task.get("required_capabilities", [])),
                    json.dumps(task.get("verification", {})),
                    task.get("status", "pending"),
                    task.get("branch"),
                    task.get("commit_sha"),
                    json.dumps(task.get("artifact_refs", [])),
                    json.dumps(task.get("metadata", {})),
                    now, now,
                ),
            )

    def list_plan_tasks(self, plan_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM composer_plan_tasks WHERE plan_id = ? ORDER BY created_at",
                (plan_id,),
            ).fetchall()
        return [self._row_to_plan_task(r) for r in rows]

    def get_plan_task(self, plan_task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM composer_plan_tasks WHERE id = ?", (plan_task_id,)
            ).fetchone()
        return self._row_to_plan_task(row) if row else None

    def get_plan_task_by_node(self, plan_id: str, node_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM composer_plan_tasks WHERE plan_id = ? AND node_key = ?",
                (plan_id, node_key),
            ).fetchone()
        return self._row_to_plan_task(row) if row else None

    def update_plan_task(
        self,
        plan_task_id: str,
        *,
        conductor_task_id: str | None = None,
        agents_gateway_task_id: str | None = None,
        status: str | None = None,
        branch: str | None = None,
        commit_sha: str | None = None,
        artifacts: list | None = None,
        metadata: dict | None = None,
        title: str | None = None,
        goal: str | None = None,
        ownership_notes: str | None = None,
        previous_status: str | None = None,
        paused_at: str | None = None,
    ) -> dict | None:
        now = _now_iso()
        sets: list[str] = []
        params: list = []
        if conductor_task_id is not None:
            sets.append("conductor_task_id = ?")
            params.append(conductor_task_id)
        if agents_gateway_task_id is not None:
            sets.append("agents_gateway_task_id = ?")
            params.append(agents_gateway_task_id)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if branch is not None:
            sets.append("branch = ?")
            params.append(branch)
        if commit_sha is not None:
            sets.append("commit_sha = ?")
            params.append(commit_sha)
        if artifacts is not None:
            sets.append("artifact_refs_json = ?")
            params.append(json.dumps(artifacts))
        if metadata is not None:
            sets.append("metadata_json = ?")
            params.append(json.dumps(metadata))
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if goal is not None:
            sets.append("goal = ?")
            params.append(goal)
        if ownership_notes is not None:
            sets.append("ownership_notes = ?")
            params.append(ownership_notes)
        if previous_status is not None:
            sets.append("previous_status = ?")
            params.append(previous_status)
        if paused_at is not None:
            sets.append("paused_at = ?")
            params.append(paused_at)
        params.append(now)
        params.append(plan_task_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE composer_plan_tasks SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                params,
            )
            conn.commit()
        return self.get_plan_task(plan_task_id)

    # ── Interaction Decisions ──────────────────────────────────────────

    def create_interaction_decision(
        self,
        objective_id: str,
        action: str,
        reply: str,
        decision_summary: str = "",
        plan_task_id: str | None = None,
        agents_gateway_interaction_id: str | None = None,
    ) -> dict:
        decision_id = f"idec_{_uid()}"
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO composer_interaction_decisions
                   (id, objective_id, plan_task_id, agents_gateway_interaction_id,
                    action, reply, decision_summary, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    decision_id, objective_id, plan_task_id,
                    agents_gateway_interaction_id, action, reply,
                    decision_summary, now,
                ),
            )
            conn.commit()
        return {  # type: ignore
            "id": decision_id,
            "objective_id": objective_id,
            "plan_task_id": plan_task_id,
            "agents_gateway_interaction_id": agents_gateway_interaction_id,
            "action": action,
            "reply": reply,
            "decision_summary": decision_summary,
            "created_at": now,
        }

    def list_interaction_decisions(self, objective_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM composer_interaction_decisions WHERE objective_id = ? ORDER BY created_at",
                (objective_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Reports ───────────────────────────────────────────────────────

    def create_report(
        self,
        objective_id: str,
        status: str,
        html_artifact_ref: str = "",
        json_artifact_ref: str = "",
        final_branch: str = "",
        final_commit_sha: str = "",
        pr_url: str | None = None,
    ) -> dict:
        report_id = f"report_{_uid()}"
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO composer_reports
                   (id, objective_id, status, html_artifact_ref, json_artifact_ref,
                    final_branch, final_commit_sha, pr_url, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    report_id, objective_id, status, html_artifact_ref,
                    json_artifact_ref, final_branch, final_commit_sha,
                    pr_url, now,
                ),
            )
            conn.commit()
        return self.get_report(report_id)  # type: ignore

    def get_report(self, report_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM composer_reports WHERE id = ?", (report_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_report_by_objective(self, objective_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM composer_reports WHERE objective_id = ? ORDER BY created_at DESC LIMIT 1",
                (objective_id,),
            ).fetchone()
        return dict(row) if row else None

    # ── Row helpers ────────────────────────────────────────────────────

    def _row_to_composer_spec(self, row) -> dict:
        ns_json = row["normalized_spec_json"]
        normalized = json.loads(ns_json) if ns_json else {}
        if isinstance(normalized, dict) and "repository" in normalized:
            if not isinstance(normalized.get("repository"), dict):
                normalized["repository"] = {}
        return {
            "id": row["id"],
            "objective_id": row["objective_id"],
            "title": row["title"],
            "raw_spec": row["raw_spec"],
            "normalized_spec": normalized,
            "repository_url": row["repository_url"],
            "base_branch": row["base_branch"],
            "status": row["status"],
            "previous_status": row["previous_status"] if "previous_status" in row.keys() else None,
            "paused_at": row["paused_at"] if "paused_at" in row.keys() else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_composer_plan(self, row) -> dict:
        plan_json = row["plan_json"]
        plan = json.loads(plan_json) if plan_json else {}
        task_rows = self.list_plan_tasks(row["id"])
        plan["plan_tasks"] = task_rows
        return {
            "id": row["id"],
            "objective_id": row["objective_id"],
            "spec_id": row["spec_id"],
            "version": row["version"],
            "status": row["status"],
            "plan_json": plan_json,
            "plan": plan,
            "plan_tasks": task_rows,
            "created_at": row["created_at"],
            "activated_at": row["activated_at"],
            "completed_at": row["completed_at"],
        }

    def _row_to_plan_task(self, row) -> dict:
        return {
            "id": row["id"],
            "plan_id": row["plan_id"],
            "node_key": row["node_key"],
            "conductor_task_id": row["conductor_task_id"],
            "agents_gateway_task_id": row["agents_gateway_task_id"],
            "task_type": row["task_type"],
            "title": row["title"] if "title" in row.keys() else "",
            "goal": row["goal"] if "goal" in row.keys() else "",
            "ownership_notes": row["ownership_notes"] if "ownership_notes" in row.keys() else "",
            "dependencies": json.loads(row["dependencies_json"]),
            "file_scope": json.loads(row["file_scope_json"]),
            "harness_profile": row["harness_profile"],
            "required_skills": json.loads(row["required_skills_json"]),
            "required_capabilities": json.loads(row["required_capabilities_json"]),
            "verification": json.loads(row["verification_json"]),
            "status": row["status"],
            "branch": row["branch"],
            "commit_sha": row["commit_sha"],
            "artifact_refs": json.loads(row["artifact_refs_json"]),
            "metadata": json.loads(row["metadata_json"]),
            "previous_status": row["previous_status"] if "previous_status" in row.keys() else None,
            "paused_at": row["paused_at"] if "paused_at" in row.keys() else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
