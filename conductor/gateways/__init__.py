"""Gateway Hub — Conductor's registry of downstream capability providers.

Conductor acts as the single hub cockpits connect to. Downstream gateways
(Agents, Skills, MCP, wiki, custom) are registered here so operators and
cockpits can ask: which gateways exist, are they healthy, what can they do.

This package is deliberately self-contained: it owns models, registry,
health checks, and capability discovery. The HTTP and MCP surfaces in
conductor.server / conductor.mcp_tools simply delegate to the registry.
"""

from conductor.gateways.models import (
    GatewayConfig,
    GatewayStatus,
    GatewayKind,
    GATEWAY_KINDS,
    GATEWAY_STATUSES,
)
from conductor.gateways.registry import GatewayRegistry, build_default_registry
from conductor.gateways.capabilities import (
    CapabilityEntry,
    STATIC_CAPABILITIES,
    list_capabilities,
    list_capabilities_by_gateway,
    find_gateways_for_capability,
)

__all__ = [
    "GatewayConfig",
    "GatewayStatus",
    "GatewayKind",
    "GATEWAY_KINDS",
    "GATEWAY_STATUSES",
    "GatewayRegistry",
    "build_default_registry",
    "CapabilityEntry",
    "STATIC_CAPABILITIES",
    "list_capabilities",
    "list_capabilities_by_gateway",
    "find_gateways_for_capability",
]
