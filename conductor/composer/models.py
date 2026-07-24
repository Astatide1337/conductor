"""Structured Composer objects, enums, and Pydantic models."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Specification ──────────────────────────────────────────────────────────

SPEC_STATUSES = (
    "received",
    "normalizing",
    "normalized",
    "planning",
    "planned",
    "executing",
    "integrating",
    "verifying",
    "completed",
    "blocked_external",
    "failed",
    "cancelled",
)

SPEC_TERMINAL = frozenset({"completed", "failed", "cancelled"})


class SpecRepository(BaseModel):
    url: str = ""
    owner: str = ""
    name: str = ""
    base_branch: str = "master"
    required: bool = True  # if True, inaccessible repo blocks objective


class NormalizedSpec(BaseModel):
    goal: str = ""
    repository: SpecRepository = Field(default_factory=SpecRepository)
    requirements: list[str] = []
    acceptance_criteria: list[str] = []
    required_live_verification: list[dict] = []
    constraints: list[str] = []
    non_goals: list[str] = []


class ComposerSpec(BaseModel):
    id: str
    objective_id: str
    title: str
    raw_spec: str = ""
    normalized_spec: NormalizedSpec = Field(default_factory=NormalizedSpec)
    repository_url: str = ""
    base_branch: str = "master"
    status: str = "received"
    created_at: str = ""
    updated_at: str = ""


class ComposerSpecCreate(BaseModel):
    title: str
    spec: str
    repository: dict | None = None  # {"url":..., "base_branch":...}
    auto_start: bool = True
    metadata: dict = {}


# ── Plan ──────────────────────────────────────────────────────────────────

PLAN_STATUSES = (
    "draft",
    "validated",
    "active",
    "integrating",
    "completed",
    "blocked_external",
    "failed",
    "superseded",
)

PLAN_TERMINAL = frozenset({"completed", "failed", "superseded"})

TASK_NODE_STATUSES = (
    "pending",
    "ready",
    "dispatching",
    "running",
    "waiting_for_reply",
    "verifying",
    "completed",
    "blocked_external",
    "failed",
    "cancelled",
)

TASK_NODE_TERMINAL = frozenset({"completed", "blocked_external", "failed", "cancelled"})

TASK_NODE_TYPES = (
    "implementation",
    "testing",
    "documentation",
    "migration",
    "integration",
    "verification",
    "research",
)


class VerificationCommand(BaseModel):
    name: str = ""
    command: str = ""
    required: bool = True
    # Runtime evidence fields (populated from Agents Gateway responses)
    exit_code: int | None = None
    blocked: bool = False
    blocked_reason: str = ""
    output_artifact: str = ""
    duration_seconds: float | None = None


class VerificationSpec(BaseModel):
    required: bool = True
    commands: list[VerificationCommand] = []
    live_e2e: dict | None = None


class TaskNode(BaseModel):
    node_id: str
    title: str = ""
    task_type: str = "implementation"
    goal: str = ""
    dependencies: list[str] = []
    file_scope: list[str] = []
    ownership_notes: str = ""
    harness_profile: str = "pi-coding-agent"
    # Optional model override passed to the harness via its
    # model_arg_name flag (e.g. --model for PI, -m for opencode).
    # Empty string means "use the profile's default_model".
    model: str = ""
    required_skills: list[str] = []
    required_capabilities: list[str] = []
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    conductor_task_id: str | None = None
    agents_gateway_task_id: str | None = None
    status: str = "pending"
    branch: str | None = None
    commit_sha: str | None = None
    artifact_refs: list[dict] = []
    metadata: dict = {}


class IntegrationNode(BaseModel):
    required: bool = True
    node_id: str = "integration"
    title: str = "Integrate completed task branches"
    task_type: str = "integration"
    goal: str = ""
    ownership_notes: str = ""
    dependencies: list[str] = []
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    status: str = "pending"
    conductor_task_id: str | None = None
    agents_gateway_task_id: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    harness_profile: str = "pi-coding-agent"
    # Optional per-integration model override (see TaskNode.model).
    model: str = ""


class ComposerPlan(BaseModel):
    id: str
    objective_id: str
    spec_id: str
    version: int = 1
    status: str = "draft"
    tasks: list[TaskNode] = []
    integration: IntegrationNode | None = None
    created_at: str = ""
    activated_at: str | None = None
    completed_at: str | None = None


# ── LLM result models ─────────────────────────────────────────────────────

class LLMTaskNode(BaseModel):
    node_id: str
    title: str = ""
    task_type: str = "implementation"
    goal: str = ""
    dependencies: list[str] = []
    file_scope: list[str] = []
    ownership_notes: str = ""
    harness_profile: str = "pi-coding-agent"
    # Optional model override (e.g. ``nvidia/nemotron-3-ultra-550b-a55b:free``).
    # The planner LLM fills this in per task so the right model is
    # passed to the harness for the job.
    model: str = ""
    required_skills: list[str] = []
    required_capabilities: list[str] = []
    verification: VerificationSpec = Field(default_factory=VerificationSpec)


class LLMIntegrationNode(BaseModel):
    required: bool = True
    node_id: str = "integration"
    title: str = "Integrate completed task branches"
    dependencies: list[str] = []
    verification: VerificationSpec = Field(default_factory=VerificationSpec)


class NormalizedSpecResult(BaseModel):
    title: str = ""
    goal: str = ""
    repository: dict = {}
    requirements: list[str] = []
    acceptance_criteria: list[str] = []
    required_live_verification: list[dict] = []
    constraints: list[str] = []
    non_goals: list[str] = []


class PlanResult(BaseModel):
    summary: str = ""
    tasks: list[LLMTaskNode] = []
    integration: LLMIntegrationNode = Field(default_factory=LLMIntegrationNode)


class InteractionResult(BaseModel):
    action: str = "reply"  # reply | redirect | restart_task | mark_external_blocker
    reply: str = ""
    decision_summary: str = ""


class FinalSummaryResult(BaseModel):
    summary: str = ""
    assumptions: list[str] = []
    blockers: list[str] = []


# ── Interaction decision ──────────────────────────────────────────────────

INTERACTION_ACTIONS = ("reply", "redirect", "restart_task", "mark_external_blocker")


class InteractionDecision(BaseModel):
    interaction_id: str
    task_node_id: str
    action: str = "reply"
    reply: str = ""
    reasoning_summary: str = ""
    created_at: str = ""


# ─<arg_value> Report ────────────────────────────────────────────────────────────────

REPORT_STATUSES = ("completed", "blocked_external", "failed", "cancelled")


class ComposerReport(BaseModel):
    id: str
    objective_id: str
    status: str = "completed"
    html_artifact_ref: str = ""
    json_artifact_ref: str = ""
    final_branch: str = ""
    final_commit_sha: str = ""
    pr_url: str | None = None
    created_at: str = ""


# ── Context ────────────────────────────────────────────────────────────────


class GatewayInfo(BaseModel):
    id: str = ""
    name: str = ""
    kind: str = ""
    enabled: bool = False
    configured: bool = False
    status: str = "unknown"


class CapabilityInfo(BaseModel):
    capability: str = ""
    gateway_id: str = ""
    available: bool = False


class HarnessProfileInfo(BaseModel):
    name: str = ""
    harness: str = ""
    display_name: str = ""
    configured: bool = False
    runnable: bool = False
    binary_present: bool = False
    credentials_present: bool = False
    command: str = ""


class SkillInfo(BaseModel):
    id: str = ""
    name: str = ""
    description: str = ""
    tags: list[str] = []


class ComposerContext(BaseModel):
    spec: dict = {}
    repository: dict = {}
    project_context: dict = {}
    gateways: list[GatewayInfo] = []
    capabilities: list[CapabilityInfo] = []
    harness_profiles: list[HarnessProfileInfo] = []
    skills: list[SkillInfo] = []
    memory: list[dict] = []


# ── Validation ────────────────────────────────────────────────────────────


class PlanValidationResult(BaseModel):
    valid: bool
    errors: list[str] = []
    warnings: list[str] = []
