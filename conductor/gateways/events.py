"""Gateway event helpers — thin wrapper over conductor.events.emit for
gateway lifecycle / action events.

The full list of gateway.* event types is:

- gateway.registered        (registry construction — emitted once at startup)
- gateway.health_checked    (per-gateway health probe that succeeded)
- gateway.health_failed     (per-gateway health probe that FAILED)
- gateway.capabilities_loaded (capability registry reload — currently static)
- gateway.capability_unavailable  (capability required by a task but no gateway provides it)
- gateway.agents.dispatch    (task dispatched to Agents Gateway — payload has agent_id, gw_task_id)
- gateway.skills.validate    (skills validated against Skills Gateway)
- gateway.mcp.tool_call      (MCP Gateway tool call issued)
- gateway.wiki.context_read (project-context read from wiki-mcp)
- gateway.actions_total      (counter-style audit; bumped on every gateway action)

Per the spec, we do NOT spam events for background checks. These helpers
are called only by user-triggered HTTP routes and objective-related
dispatch paths.
"""

from __future__ import annotations

from typing import Optional

from conductor.events import emit
from conductor.storage import ConductorStorage


def _emit_gateway_event(
    storage: ConductorStorage,
    event_type: str,
    message: str,
    *,
    gateway_id: str,
    gateway_kind: str,
    payload: dict | None = None,
    objective_id: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    source: str = "conductor",
) -> None:
    full_payload = {"gateway_id": gateway_id, "gateway_kind": gateway_kind}
    if payload:
        full_payload.update(payload)
    emit(
        storage, event_type, message,
        objective_id=objective_id, run_id=run_id, task_id=task_id,
        payload=full_payload, source=source,
    )


def emit_gateway_registered(
    storage: ConductorStorage,
    gateway_id: str, gateway_kind: str, name: str,
) -> None:
    _emit_gateway_event(
        storage, "gateway.registered", f"Gateway registered: {name} ({gateway_id})",
        gateway_id=gateway_id, gateway_kind=gateway_kind,
        payload={"name": name},
    )


def emit_gateway_health_checked(
    storage: ConductorStorage,
    gateway_id: str, gateway_kind: str,
    *,
    status: str, latency_ms: float | None = None,
    capabilities: list[str] | None = None,
) -> None:
    success = status == "healthy"
    p: dict = {"status": status}
    if latency_ms is not None:
        p["latency_ms"] = latency_ms
    if capabilities is not None:
        p["capabilities"] = capabilities
    _emit_gateway_event(
        storage,
        "gateway.health_checked" if success else "gateway.health_failed",
        f"Gateway {gateway_id} health: {status}",
        gateway_id=gateway_id, gateway_kind=gateway_kind, payload=p,
    )


def emit_gateway_capabilities_loaded(
    storage: ConductorStorage,
    gateway_id: str, gateway_kind: str,
    capabilities: list[str],
) -> None:
    _emit_gateway_event(
        storage, "gateway.capabilities_loaded",
        f"Loaded {len(capabilities)} capabilities for gateway {gateway_id}",
        gateway_id=gateway_id, gateway_kind=gateway_kind,
        payload={"capabilities": capabilities},
    )


def emit_gateway_capability_unavailable(
    storage: ConductorStorage,
    capability: str,
    *,
    objective_id: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> None:
    emit(
        storage, "gateway.capability_unavailable",
        f"Capability '{capability}' unavailable — no gateway provides it",
        objective_id=objective_id, run_id=run_id, task_id=task_id,
        payload={"capability": capability}, source="conductor",
    )


def emit_gateway_agents_dispatch(
    storage: ConductorStorage,
    *,
    gateway_id: str,
    agent_id: str,
    agents_gateway_task_id: str,
    objective_id: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> None:
    _emit_gateway_event(
        storage, "gateway.agents.dispatch",
        f"Dispatched task to agents gateway {gateway_id} (gw_task={agents_gateway_task_id})",
        gateway_id=gateway_id, gateway_kind="agents",
        payload={"agent_id": agent_id, "agents_gateway_task_id": agents_gateway_task_id},
        objective_id=objective_id, run_id=run_id, task_id=task_id,
    )


def emit_gateway_skills_validate(
    storage: ConductorStorage,
    *,
    gateway_id: str,
    required_skills: list[str],
    missing_skills: list[str] | None = None,
    objective_id: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> None:
    p: dict = {"required_skills": required_skills}
    if missing_skills is not None:
        p["missing_skills"] = missing_skills
    _emit_gateway_event(
        storage, "gateway.skills.validate",
        f"Validated skills against skills gateway {gateway_id}",
        gateway_id=gateway_id, gateway_kind="skills",
        payload=p,
        objective_id=objective_id, run_id=run_id, task_id=task_id,
    )


def emit_gateway_mcp_tool_call(
    storage: ConductorStorage,
    *,
    gateway_id: str,
    tool_name: str,
    arguments: dict | None = None,
    objective_id: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> None:
    p: dict = {"tool_name": tool_name, "arguments": arguments or {}}
    _emit_gateway_event(
        storage, "gateway.mcp.tool_call",
        f"MCP Gateway {gateway_id} tool call: {tool_name}",
        gateway_id=gateway_id, gateway_kind="mcp",
        payload=p,
        objective_id=objective_id, run_id=run_id, task_id=task_id,
    )


def emit_gateway_wiki_context_read(
    storage: ConductorStorage,
    *,
    gateway_id: str,
    objective_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> None:
    _emit_gateway_event(
        storage, "gateway.wiki.context_read",
        f"Read project context from wiki-mcp {gateway_id}",
        gateway_id=gateway_id, gateway_kind="wiki",
        objective_id=objective_id, run_id=run_id,
    )
