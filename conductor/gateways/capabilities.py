"""Capability catalog — the things a gateway kind can do for Conductor.

Capabilities are deliberately namespaced with a dotted prefix:

- execution.*     — Agents Gateway execution surface
- skills.*        — Skills Gateway surface
- tools.*         — MCP Gateway tool catalog and routing
- external.*      — MCP Gateway-bridged external services
- memory.*        — wiki-mcp durable memory
- context.*       — wiki-mcp project context

A capability lookup should be deterministic in dev (when real downstream
discovery is not available). Downstream gateways may expose richer dynamic
capability metadata in production; for this milestone we use a static
mapping keyed on the gateway kind so the catalog is always consistent.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from pydantic import BaseModel


class CapabilityEntry(BaseModel):
    """Single capability descriptor."""
    capability: str
    gateway_id: str
    gateway_kind: str
    available: bool = True
    source: str = "static"  # static | discovered
    description: str = ""


def _entries(
    gateway_id: str,
    gateway_kind: str,
    caps: list[tuple[str, str]],
) -> list[CapabilityEntry]:
    return [
        CapabilityEntry(
            capability=cap,
            gateway_id=gateway_id,
            gateway_kind=gateway_kind,
            description=desc,
        )
        for cap, desc in caps
    ]


# ── Static capability catalog by gateway kind ──────────────────────────────
STATIC_CAPABILITIES: dict[str, list[tuple[str, str]]] = {
    "agents": [
        ("execution.task.create", "Create execution tasks through Agents Gateway."),
        ("execution.task.run", "Run queued tasks through Agents Gateway."),
        ("execution.task.status", "Read task execution status."),
        ("execution.events.read", "Read task execution events."),
        ("execution.artifacts.read", "Read execution artifacts produced by tasks."),
    ],
    "skills": [
        ("skills.list", "List reusable skills registered on Skills Gateway."),
        ("skills.inspect", "Inspect metadata for a single skill."),
        ("skills.validate", "Validate required skills against the Skills Gateway."),
        ("skills.read", "Read full skill text / instructions / methodology."),
    ],
    "mcp": [
        ("tools.list", "Discover MCP tools exposed by the downstream MCP Gateway."),
        ("tools.call", "Invoke a downstream MCP tool by name."),
        ("connectors.route", "Route a request to an external connector via MCP Gateway."),
        ("external.github", "GitHub external connector hosted on MCP Gateway."),
        ("external.drive", "Drive external connector hosted on MCP Gateway."),
        ("external.calendar", "Calendar external connector hosted on MCP Gateway."),
        ("external.mail", "Mail external connector hosted on MCP Gateway."),
    ],
    "wiki": [
        ("memory.read", "Read durable memory entries from wiki-mcp."),
        ("memory.write", "Append durable memory entries to wiki-mcp."),
        ("memory.search", "Search durable memory on wiki-mcp."),
        ("context.project", "Read project context / decision logs from wiki-mcp."),
    ],
    "custom": [],
}


def list_capabilities(
    registry,
    *,
    gateway_id: str | None = None,
    only_available: bool = True,
) -> list[CapabilityEntry]:
    """Return capability entries for all gateways in the registry.

    A capability is `available=True` when its gateway is both configured
    and enabled (URL present, enabled=True). Capabilities for not-configured
    or disabled gateways are still listed with `available=False` so operators
    can see "what would become available if we enabled this gateway".
    """
    gateways = [registry.get(gateway_id)] if gateway_id else registry.all()
    entries: list[CapabilityEntry] = []

    for gw in gateways:
        if gw is None:
            continue
        kind_caps = STATIC_CAPABILITIES.get(gw.kind, [])
        gw_configured = bool(gw.base_url)
        gw_enabled = gw.enabled
        for cap, desc in kind_caps:
            avail = gw_configured and gw_enabled
            entries.append(
                CapabilityEntry(
                    capability=cap,
                    gateway_id=gw.id,
                    gateway_kind=gw.kind,
                    available=avail if only_available else True,
                    source="static",
                    description=desc,
                )
            )
    return entries


def list_capabilities_by_gateway(registry, gateway_id: str) -> list[CapabilityEntry]:
    return list_capabilities(registry, gateway_id=gateway_id)


def find_gateways_for_capability(registry, capability: str) -> list[CapabilityEntry]:
    """Return all capability entries whose `capability` matches.

    Multiple gateways may expose the same capability; the registry returns
    every match so callers can decide (by policy, health, or load).
    """
    matches: list[CapabilityEntry] = []
    for gw in registry.all():
        for cap, _desc in STATIC_CAPABILITIES.get(gw.kind, []):
            if cap == capability:
                gw_configured = bool(gw.base_url)
                gw_enabled = gw.enabled
                matches.append(
                    CapabilityEntry(
                        capability=cap,
                        gateway_id=gw.id,
                        gateway_kind=gw.kind,
                        available=gw_configured and gw_enabled,
                        source="static",
                    )
                )
    return matches


def capabilities_for_kind(kind: str) -> list[str]:
    """Return just the capability names for a gateway kind."""
    return [cap for cap, _ in STATIC_CAPABILITIES.get(kind, [])]
