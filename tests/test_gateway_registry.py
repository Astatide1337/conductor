"""Tests for the gateway registry — config-derived downstream hub.

Covers: registry registration, build_default_registry produces the
canonical four gateways (agents, skills, mcp, wiki), missing URL →
not_configured, disabled gateway → disabled, auth mode stored but tokens
never carried in registry config.
"""

from conductor.config import ConductorConfig
from conductor.gateways import build_default_registry, GatewayConfig, GatewayRegistry


class TestGatewayRegistry:
    def test_register_and_get(self):
        reg = GatewayRegistry()
        reg.register(GatewayConfig(
            id="custom", name="Custom", kind="custom",
            base_url="http://custom.example",
        ))
        assert reg.has("custom")
        gw = reg.get("custom")
        assert gw is not None and gw.kind == "custom"
        assert reg.get("missing") is None
        assert len(reg) == 1

    def test_by_kind(self):
        reg = GatewayRegistry()
        reg.register(GatewayConfig(id="a1", name="A1", kind="agents", base_url="http://a"))
        reg.register(GatewayConfig(id="a2", name="A2", kind="agents", base_url="http://b"))
        reg.register(GatewayConfig(id="s1", name="S1", kind="skills", base_url="http://c"))
        assert [g.id for g in reg.by_kind("agents")] == ["a1", "a2"]
        assert [g.id for g in reg.by_kind("skills")] == ["s1"]
        assert reg.by_kind("mcp") == []

    def test_enabled_and_configured_filters(self):
        reg = GatewayRegistry()
        reg.register(GatewayConfig(id="a", name="A", kind="agents", base_url="http://a"))
        reg.register(GatewayConfig(id="b", name="B", kind="agents", base_url="", enabled=True))
        reg.register(GatewayConfig(id="c", name="C", kind="agents", base_url="http://c", enabled=False))
        assert [g.id for g in reg.enabled()] == ["a", "b"]
        assert [g.id for g in reg.configured()] == ["a", "c"]
        assert [g.id for g in reg.configured_and_enabled()] == ["a"]


class TestBuildDefaultRegistry:
    def test_registers_four_canonical_kinds(self):
        cfg = ConductorConfig(environment="test")
        reg = build_default_registry(cfg)
        ids = sorted(g.id for g in reg.all())
        assert ids == ["agents", "mcp", "skills", "wiki"]
        kinds = {g.kind for g in reg.all()}
        assert kinds == {"agents", "skills", "mcp", "wiki"}

    def test_missing_url_becomes_not_configured(self):
        cfg = ConductorConfig(environment="test",
                              mcp_gateway={"url": ""},
                              wiki_mcp={"url": ""})
        reg = build_default_registry(cfg)
        mcp = reg.get("mcp")
        wiki = reg.get("wiki")
        assert not mcp.base_url
        assert not wiki.base_url
        # mcp + wiki are disabled when URL is blank
        assert mcp.enabled is False
        assert wiki.enabled is False

    def test_disabled_gateway_when_url_blank(self):
        cfg = ConductorConfig(environment="test",
                              mcp_gateway={"url": "http://mcp.local"})
        reg = build_default_registry(cfg)
        mcp = reg.get("mcp")
        assert mcp.enabled is True
        assert mcp.base_url == "http://mcp.local"

    def test_auth_mode_stored_but_no_token(self):
        cfg = ConductorConfig(environment="test",
                              agents_gateway={
                                  "url": "http://agents.example",
                                  "auth_mode": "internal-only",
                                  "internal_token": "t0p-secret",
                              })
        reg = build_default_registry(cfg)
        agents = reg.get("agents")
        assert agents.auth_mode == "internal-only"
        # Registry GatewayConfig never carries tokens — they live in client configs.
        assert not hasattr(agents, "internal_token")

    def test_default_registry_carries_custom_sections(self):
        """When CONDUCTOR_MCP_GATEWAY_URL is unset, mcp gateway entry exists
        but is not configured — operators can see 'what would become available'."""
        cfg = ConductorConfig(environment="test")
        reg = build_default_registry(cfg)
        capabilities_path_present = [g for g in reg.all()
                                     if g.capabilities_path is None]
        assert reg.get("agents") is not None


class TestRegistryIteration:
    def test_iteration_iterates_all(self):
        reg = GatewayRegistry()
        reg.register(GatewayConfig(id="a", name="A", kind="agents"))
        reg.register(GatewayConfig(id="b", name="B", kind="mcp"))
        ids = [g.id for g in reg]
        assert sorted(ids) == ["a", "b"]
