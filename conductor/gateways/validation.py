"""Capability validation — checks a task's required gateway capabilities against
the Gateway Hub registry.

A capability is satisfied when AT LEAST ONE configured+enabled+healthy
gateway exposes it. The three-step check is:

  1. Capability resolution: find candidate gateways
  2. For each candidate: ensure the gateway is configured AND enabled
  3. If `require_healthy=True` (default), the gateway must also be health
     -checked healthy in the most recent probe.

Per the spec, this milestone implements validation but does NOT
"over-enforce" — policy (block vs degrade vs allow) is up to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from conductor.gateways.capabilities import find_gateways_for_capability
from conductor.gateways.registry import GatewayRegistry
from conductor.gateways.models import GatewayStatus


@dataclass
class CapabilityValidationResult:
    valid: bool
    missing: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)
    by_capability: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "missing": self.missing,
            "degraded": self.degraded,
            "satisfied": self.satisfied,
            "by_capability": self.by_capability,
        }


def validate_required_capabilities(
    registry: GatewayRegistry,
    required_capabilities: list[str],
    latest_status: dict[str, GatewayStatus] | None = None,
    *,
    require_healthy: bool = False,
) -> CapabilityValidationResult:
    """Validate that every capability has at least one candidate gateway.

    Args:
        registry: gateway registry
        required_capabilities: list of capability strings (dotted) to check
        latest_status: optional dict of gateway_id → most recent GatewayStatus
                       (used to filter unhealthy candidates when require_healthy=True)
        require_healthy: if True, capability whose only candidates are
                          unhealthy / disabled / not_configured → mark as missing

    Returns a CapabilityValidationResult; never raises.
    """
    if not required_capabilities:
        return CapabilityValidationResult(valid=True)

    missing: list[str] = []
    degraded: list[str] = []
    satisfied: list[str] = []
    by_cap: dict = {}

    for cap in required_capabilities:
        candidates = find_gateways_for_capability(registry, cap)

        # Available candidates: configured + enabled.
        avail = [c for c in candidates if c.available]

        # If require_healthy, narrow further by last-known health status.
        if require_healthy and latest_status is not None:
            avail = [
                c for c in avail
                if latest_status.get(c.gateway_id) is not None
                and latest_status[c.gateway_id].status == "healthy"
            ]

        by_cap[cap] = [c.model_dump() for c in avail]

        if not avail:
            # Distinguish degraded (capability exists, all providers unhealthy/disabled)
            # from missing (no gateway exposes it at all).
            if candidates:
                degraded.append(cap)
            else:
                missing.append(cap)
        else:
            satisfied.append(cap)

    valid = (not missing) and (not degraded) if require_healthy else (not missing)
    return CapabilityValidationResult(
        valid=valid,
        missing=missing,
        degraded=degraded,
        satisfied=satisfied,
        by_capability=by_cap,
    )


def get_required_capabilities_from_task(task: dict) -> list[str]:
    """Pull `required_capabilities` from task metadata if present."""
    if not task:
        return []
    md = task.get("metadata") or {}
    caps = md.get("required_capabilities") or []
    if not isinstance(caps, list):
        return []
    return [c for c in caps if isinstance(c, str)]


def get_gateway_dependencies_from_task(task: dict) -> list[str]:
    md = task.get("metadata") or {}
    deps = md.get("gateway_dependencies") or []
    if not isinstance(deps, list):
        return []
    return [d for d in deps if isinstance(d, str)]
