"""Pydantic data models for the Conductor domain.

Objective, Run, Task, AgentRun, Approval, PlannerTurn, CostEntry
"""

import json
from datetime import UTC, datetime
from typing import Optional

from pydantic import BaseModel


# ── Objective ──────────────────────────────────────────────────────────────

OBJECTIVE_STATUSES = {"created", "active", "paused", "blocked", "completed", "failed", "cancelled"}
TERMINAL_OBJECTIVE_STATUSES = {"completed", "failed", "cancelled"}

OBJECTIVE_TRANSITIONS: dict[str, set[str]] = {
    "created": {"active"},
    "active": {"paused", "blocked", "completed", "failed", "cancelled"},
    "paused": {"active"},
    "blocked": {"active", "failed"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


class Objective(BaseModel):
    id: str
    title: str
    description: str = ""
    status: str = "created"
    priority: str = "normal"
    created_at: str
    updated_at: str
    created_by: str = ""
    metadata: dict = {}


class ObjectiveCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "normal"
    created_by: str = ""
    metadata: dict = {}


class ObjectiveSummary(BaseModel):
    id: str
    title: str
    status: str
    priority: str
    created_at: str
    updated_at: str


# ── Objective Run ───────────────────────────────────────────────────────────

RUN_STATUSES = OBJECTIVE_STATUSES  # same set

RUN_TRANSITIONS = OBJECTIVE_TRANSITIONS  # same rules


class ObjectiveRun(BaseModel):
    id: str
    objective_id: str
    status: str = "created"
    started_at: str | None = None
    finished_at: str | None = None
    planner_mode: str = "manual"
    max_iterations: int = 50
    max_cost_usd: float = 10.0
    max_concurrent_tasks: int = 4
    metadata: dict = {}


class ObjectiveRunCreate(BaseModel):
    planner_mode: str = "manual"
    max_iterations: int = 50
    max_cost_usd: float = 10.0
    max_concurrent_tasks: int = 4
    metadata: dict = {}


# ── Task ────────────────────────────────────────────────────────────────────

TASK_STATUSES = {"created", "ready", "dispatched", "running", "blocked", "completed", "failed", "cancelled"}
TASK_TERMINAL = {"completed", "failed", "cancelled"}

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

TASK_TYPES = {"ship", "scout", "verify", "review", "docs", "ops"}


class Task(BaseModel):
    id: str
    objective_id: str
    run_id: str
    title: str
    brief: str = ""
    status: str = "created"
    task_type: str = "ship"
    depends_on: list[str] = []
    required_skills: list[str] = []
    dispatch_profile: str = ""
    approval_required: bool = False
    created_at: str
    updated_at: str
    metadata: dict = {}


class TaskCreate(BaseModel):
    title: str
    brief: str = ""
    task_type: str = "ship"
    depends_on: list[str] = []
    required_skills: list[str] = []
    dispatch_profile: str = ""
    approval_required: bool = False
    metadata: dict = {}


# ── Agent Run ───────────────────────────────────────────────────────────────

AGENT_RUN_STATUSES = {"created", "dispatched", "queued", "running", "completed", "failed", "cancelled", "lost"}


class AgentRun(BaseModel):
    id: str
    task_id: str
    agents_gateway_task_id: str | None = None
    attempt: int = 1
    status: str = "created"
    idempotency_key: str = ""
    dispatch_profile: str = ""
    runtime_type: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    last_reconciled_at: str | None = None
    result_summary: str = ""
    artifact_refs: list = []
    metadata: dict = {}


# ── Approval ────────────────────────────────────────────────────────────────

APPROVAL_STATUSES = {"pending", "approved", "rejected", "expired", "cancelled"}

APPROVAL_ACTION_TYPES = {
    "merge_main",
    "deploy_production",
    "modify_secrets",
    "modify_cloudflare",
    "delete_data",
    "spend_money",
    "destructive_command",
    "production_db_migration",
}


class Approval(BaseModel):
    id: str
    objective_id: str
    run_id: str
    task_id: str | None = None
    action_type: str
    description: str = ""
    risk_level: str = "medium"
    status: str = "pending"
    requested_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    payload: dict = {}


class ApprovalCreate(BaseModel):
    action_type: str
    description: str = ""
    risk_level: str = "medium"
    payload: dict = {}


class ApprovalDecision(BaseModel):
    approved: bool
    reason: str = ""
    decided_by: str = ""


# ── Planner Turn ────────────────────────────────────────────────────────────


class PlannerTurn(BaseModel):
    id: str
    objective_id: str
    run_id: str
    input_summary: str = ""
    output: dict = {}
    valid: bool = True
    error: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    created_at: str


class PlannerDecision(BaseModel):
    decision_type: str
    reason: str = ""
    task_id: str | None = None
    new_tasks: list[dict] = []
    approval_request: dict | None = None
    guidance: dict | None = None
    confidence: float = 1.0


VALID_DECISION_TYPES = {
    "create_tasks",
    "dispatch_task",
    "retry_task",
    "request_approval",
    "mark_task_blocked",
    "mark_objective_blocked",
    "mark_objective_complete",
    "pause_objective",
    "do_nothing",
}


# ── Cost Entry ──────────────────────────────────────────────────────────────


class CostEntry(BaseModel):
    id: str
    objective_id: str
    run_id: str
    source: str = "planner"
    amount_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    description: str = ""
    created_at: str


class DryRunResult(BaseModel):
    proposed_tasks: list[dict] = []
    required_skills: list[str] = []
    approval_gates: list[str] = []
    estimated_risks: list[str] = []
    would_dispatch: bool = False


class GetStatusResult(BaseModel):
    objective: dict = {}
    run: dict = {}
    tasks: list[dict] = []
    agent_runs: list[dict] = []
    pending_approvals: list[dict] = []
    recent_events: list[dict] = []
    circuit_breakers: dict = {}