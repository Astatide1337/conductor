"""Gateway registry — known downstream gateways.

A `GatewayRegistry` stores `GatewayConfig` entries by id. It exposes
introspection methods (`get`, `all`, `by_kind`) and a high-level factory
`build_default_registry(cfg)` that constructs the canonical registry from
a `ConductorConfig`, including the four standard gateways (agents, skills,
mcp, wiki).

The registry has NO notion of health — that lives in
`conductor.gateways.health`. The registry is purely configuration.

Secrets redaction: `GatewayConfig.auth_mode` is stored but tokens are
never carried here — the registry derives status independently of the
auth HTTP client which holds the token in its own process state.
"""

from __future__ import annotations

from conductor.config import ConductorConfig
from conductor.gateways.models import GatewayConfig


class GatewayRegistry:
    def __init__(self) -> None:
        self._gateways: dict[str, GatewayConfig] = {}

    def register(self, config: GatewayConfig) -> None:
        self._gateways[config.id] = config

    def has(self, gateway_id: str) -> bool:
        return gateway_id in self._gateways

    def get(self, gateway_id: str) -> GatewayConfig | None:
        return self._gateways.get(gateway_id)

    def all(self) -> list[GatewayConfig]:
        return list(self._gateways.values())

    def by_kind(self, kind: str) -> list[GatewayConfig]:
        return [g for g in self._gateways.values() if g.kind == kind]

    def enabled(self) -> list[GatewayConfig]:
        return [g for g in self._gateways.values() if g.enabled]

    def configured(self) -> list[GatewayConfig]:
        """All gateways with a non-empty base_url (regardless of enabled)."""
        return [g for g in self._gateways.values() if g.base_url]

    def configured_and_enabled(self) -> list[GatewayConfig]:
        return [g for g in self._gateways.values() if g.enabled and g.base_url]

    def __len__(self) -> int:
        return len(self._gateways)

    def __iter__(self):
        return iter(self._gateways.values())


def build_default_registry(cfg: ConductorConfig) -> GatewayRegistry:
    """Construct the canonical downstream gateway registry from config.

    Always registers the four standard gateway kinds even when not configured.
    A blank `base_url` produces a `not_configured` status during health
    checks — useful so operators see "what gateways *could* be plugged in".
    """
    reg = GatewayRegistry()

    reg.register(GatewayConfig(
        id="agents",
        name="Agents Gateway",
        kind="agents",
        base_url=cfg.agents_gateway.url or "",
        enabled=True,
        auth_mode=cfg.agents_gateway.auth_mode,
        health_path="/health",
        version_path="/version",
        timeout_seconds=cfg.agents_gateway.timeout_seconds,
        metadata={"client": "HttpAgentsGatewayClient"},
    ))

    reg.register(GatewayConfig(
        id="skills",
        name="Skills Gateway",
        kind="skills",
        base_url=cfg.skills_gateway.url or "",
        enabled=True,
        auth_mode=cfg.skills_gateway.auth_mode,
        health_path="/health",
        version_path="/version",
        timeout_seconds=cfg.skills_gateway.timeout_seconds,
        metadata={"client": "HttpSkillsGatewayClient"},
    ))

    reg.register(GatewayConfig(
        id="mcp",
        name="MCP Gateway",
        kind="mcp",
        base_url=cfg.mcp_gateway.url or "",
        # MCP Gateway is opt-in until CONDUCTOR_MCP_GATEWAY_URL is set.
        enabled=bool(cfg.mcp_gateway.url),
        auth_mode=cfg.mcp_gateway.auth_mode,
        health_path="/health",
        version_path="/version",
        timeout_seconds=cfg.mcp_gateway.timeout_seconds,
        metadata={"client": "HttpMcpGatewayClient"},
    ))

    reg.register(GatewayConfig(
        id="wiki",
        name="wiki-mcp",
        kind="wiki",
        base_url=cfg.wiki_mcp.url or "",
        # wiki-mcp is disabled by default — opt in via CONDUCTOR_WIKI_MCP_URL.
        enabled=bool(cfg.wiki_mcp.url),
        auth_mode=cfg.wiki_mcp.auth_mode,
        health_path="/health",
        version_path="/version",
        timeout_seconds=cfg.wiki_mcp.timeout_seconds,
        metadata={"client": "HttpWikiMcpClient"},
    ))

    return reg
