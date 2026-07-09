"""Tests for gateway capability registry — static catalog by kind."""

from conductor.gateways import GatewayConfig, GatewayRegistry
from conductor.gateways.capabilities import (
    STATIC_CAPABILITIES,
    list_capabilities,
    list_capabilities_by_gateway,
    find_gateways_for_capability,
    capabilities_for_kind,
)
from conductor.config import ConductorConfig
from conductor.gateways import build_default_registry


def _reg(*gateways) -> GatewayRegistry:
    reg = GatewayRegistry()
    for g in gateways:
        reg.register(g)
    return reg


def _agents(enabled=True, base_url="http://a") -> GatewayConfig:
    return GatewayConfig(id="agents", name="Agents", kind="agents",
                         base_url=base_url, enabled=enabled)


def _skills(enabled=True, base_url="http://s") -> GatewayConfig:
    return GatewayConfig(id="skills", name="Skills", kind="skills",
                         base_url=base_url, enabled=enabled)


def _mcp(enabled=True, base_url="") -> GatewayConfig:
    return GatewayConfig(id="mcp", name="MCP Gateway", kind="mcp",
                         base_url=base_url or "http://mcp", enabled=enabled)


def _wiki(enabled=False, base_url="") -> GatewayConfig:
    return GatewayConfig(id="wiki", name="wiki-mcp", kind="wiki",
                         base_url=base_url, enabled=enabled)


class TestStaticCapabilities:
    def test_agents_has_execution_capabilities(self):
        caps = capabilities_for_kind("agents")
        assert "execution.task.create" in caps
        assert "execution.task.run" in caps
        assert "execution.task.status" in caps

    def test_skills_has_skill_capabilities(self):
        caps = capabilities_for_kind("skills")
        assert "skills.list" in caps
        assert "skills.validate" in caps

    def test_mcp_has_tools_and_external_capabilities(self):
        caps = capabilities_for_kind("mcp")
        assert "tools.list" in caps
        assert "tools.call" in caps
        assert "external.github" in caps
        assert "external.calendar" in caps

    def test_wiki_has_memory_capabilities(self):
        caps = capabilities_for_kind("wiki")
        assert "memory.read" in caps
        assert "context.project" in caps

    def test_custom_kind_empty(self):
        assert capabilities_for_kind("custom") == []


class TestListCapabilities:
    def test_list_all_capabilities(self):
        reg = _reg(_agents(), _skills())
        caps = list_capabilities(reg)
        cap_names = {c.capability for c in caps}
        assert "execution.task.create" in cap_names
        assert "skills.validate" in cap_names
        assert len(caps) == len(STATIC_CAPABILITIES["agents"]) + len(STATIC_CAPABILITIES["skills"])

    def test_filter_by_gateway(self):
        reg = _reg(_agents(), _skills())
        caps = list_capabilities_by_gateway(reg, "agents")
        assert all(c.gateway_id == "agents" for c in caps)
        assert {c.capability for c in caps} == {cap for cap, _ in STATIC_CAPABILITIES["agents"]}

    def test_disabled_caps_marked_unavailable(self):
        reg = _reg(_agents(enabled=False, base_url="http://a"))
        caps = list_capabilities(reg)
        agents_caps = [c for c in caps if c.gateway_kind == "agents"]
        assert all(c.available is False for c in agents_caps)

    def test_not_configured_caps_unavailable(self):
        reg = _reg(_agents(base_url=""))
        caps = list_capabilities(reg, gateway_id="agents")
        assert all(c.available is False for c in caps)


class TestFindGateways:
    def test_find_execution_capability_returns_agents(self):
        reg = _reg(_agents(), _skills(), _mcp())
        hits = find_gateways_for_capability(reg, "execution.task.create")
        assert len(hits) == 1
        assert hits[0].gateway_id == "agents"
        assert hits[0].available is True

    def test_find_skills_capability_returns_skills(self):
        reg = _reg(_agents(), _skills())
        hits = find_gateways_for_capability(reg, "skills.validate")
        assert len(hits) == 1
        assert hits[0].gateway_id == "skills"

    def test_find_unknown_capability_returns_empty(self):
        reg = _reg(_agents(), _skills())
        assert find_gateways_for_capability(reg, "does.not.exist") == []

    def test_find_capability_when_disabled_returns_unavailable(self):
        reg = _reg(_agents(enabled=False))
        hits = find_gateways_for_capability(reg, "execution.task.create")
        assert len(hits) == 1
        assert hits[0].available is False


class TestBuildDefaultRegistryCapabilities:
    def test_default_registry_lists_capabilities(self):
        cfg = ConductorConfig(environment="test")
        reg = build_default_registry(cfg)
        caps = list_capabilities(reg)
        # Should have entries for every gateway/kind in the registry
        kinds_with_caps = {c.gateway_kind for c in caps}
        assert "agents" in kinds_with_caps
        assert "skills" in kinds_with_caps
        assert "mcp" in kinds_with_caps
        assert "wiki" in kinds_with_caps
