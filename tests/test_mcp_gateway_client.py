"""Tests for MCP Gateway downstream client (mock + HTTP).

Coverage:
- list_tools returns tool metadata
- call_tool sends expected payload
- 401 produces structured auth error
- 5xx retries if safe, then structured error
- timeout produces structured timeout error
- tool-call failure does not crash Conductor
- mock client behaviour
"""

import httpx
import pytest
import respx

from conductor.config import McpGatewayClientConfig
from conductor.clients.mcp_gateway import (
    MockMcpGatewayClient,
    HttpMcpGatewayClient,
    McpGatewayError,
    McpTool,
)


class TestMockMcpGatewayClient:
    def test_health(self):
        cli = MockMcpGatewayClient()
        assert cli.health()["status"] == "ok"
        cli.set_healthy(False)
        assert cli.health()["status"] == "down"

    def test_version(self):
        cli = MockMcpGatewayClient()
        v = cli.version()
        assert v["version"] == "1.0.0-test"
        assert "service" in v

    def test_list_tools_returns_metadata(self):
        cli = MockMcpGatewayClient()
        tools = cli.list_tools()
        assert len(tools) >= 1
        assert any(t.name == "github.search" for t in tools)
        assert all(isinstance(t, McpTool) for t in tools)
        assert tools[0].input_schema

    def test_call_tool_sends_expected_payload(self):
        cli = MockMcpGatewayClient()
        result = cli.call_tool("github.search", {"query": "astatide"})
        assert result["tool"] == "github.search"
        assert result["input"] == {"query": "astatide"}
        assert cli.calls[-1] == ("github.search", {"query": "astatide"})

    def test_call_tool_unknown_raises_structured_error(self):
        cli = MockMcpGatewayClient()
        with pytest.raises(McpGatewayError) as ei:
            cli.call_tool("never.exists")
        assert ei.value.status == 404
        assert "unknown tool" in str(ei.value)


@pytest.fixture
def cfg():
    return McpGatewayClientConfig(
        url="http://mcp.test",
        auth_mode="internal-only",
        internal_token="t0p",
        timeout_seconds=5.0,
    )


class TestHttpMcpGatewayClient:
    @respx.mock
    def test_health_ok(self, cfg):
        respx.get("http://mcp.test/health").respond(200, json={"status": "ok"})
        respx.get("http://mcp.test/version").respond(200, json={"version": "9.9"})
        cli = HttpMcpGatewayClient(cfg)
        assert cli.health()["status"] == "ok"
        assert cli.version()["version"] == "9.9"

    @respx.mock
    def test_list_tools_parses_response(self, cfg):
        respx.post("http://mcp.test/tools/list").respond(200, json={
            "tools": [
                {"name": "gh", "description": "GitHub", "input_schema": {"q": "str"}},
            ],
        })
        cli = HttpMcpGatewayClient(cfg)
        tools = cli.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "gh"
        assert tools[0].description == "GitHub"
        assert tools[0].input_schema == {"q": "str"}

    @respx.mock
    def test_call_tool_sends_expected_payload(self, cfg):
        route = respx.post("http://mcp.test/tools/call").respond(200, json={"result": "ok"})
        cli = HttpMcpGatewayClient(cfg)
        result = cli.call_tool("gh", {"q": "test"})
        assert result["result"] == "ok"
        sent = route.calls.last.request.read()
        assert "gh" in sent.decode()
        # Auth header included
        assert route.calls.last.request.headers.get("X-Auth-Internal-Token") == "t0p"

    @respx.mock
    def test_401_produces_structured_error(self, cfg):
        respx.post("http://mcp.test/tools/call").respond(401, json={"detail": "no"})
        cli = HttpMcpGatewayClient(cfg)
        with pytest.raises(McpGatewayError) as ei:
            cli.call_tool("gh")
        assert ei.value.status == 401
        assert ei.value.method == "POST /tools/call"
        assert ei.value.transient is False

    @respx.mock
    def test_5xx_retries_then_succeeds(self, cfg):
        route = respx.post("http://mcp.test/tools/call")
        responses = [
            httpx.Response(503, json={"detail": "down"}),
            httpx.Response(200, json={"result": "after_retry"}),
        ]
        route.mock(side_effect=responses)
        cli = HttpMcpGatewayClient(cfg, max_retries=2)
        cli._backoff = lambda attempt: None
        result = cli.call_tool("gh")
        assert result["result"] == "after_retry"
        assert route.call_count == 2

    @respx.mock
    def test_retries_exhausted_raises_structured_error(self, cfg):
        route = respx.post("http://mcp.test/tools/call")
        responses = [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
        ]
        route.mock(side_effect=responses)
        cli = HttpMcpGatewayClient(cfg, max_retries=2)
        cli._backoff = lambda attempt: None
        with pytest.raises(McpGatewayError) as ei:
            cli.call_tool("gh")
        assert ei.value.status == 503
        assert ei.value.transient is True
        # max_retries=2 means total 3 attempts → 3 route calls
        assert route.call_count == 3

    @respx.mock
    def test_timeout_produces_structured_timeout_error(self, cfg):
        respx.post("http://mcp.test/tools/call").mock(side_effect=httpx.ConnectTimeout("slow"))
        cli = HttpMcpGatewayClient(cfg, max_retries=0)
        with pytest.raises(McpGatewayError) as ei:
            cli.call_tool("gh")
        assert ei.value.transient is True
        assert "transport" in str(ei.value).lower() or "timeout" in str(ei.value).lower()

    @respx.mock
    def test_tool_call_failure_does_not_crash_conductor(self, cfg):
        """A failing tool call raises McpGatewayError — it never propagates
        beyond the client boundary. Callers wrap in their own try/except
        and emit framework events."""
        respx.post("http://mcp.test/tools/call").respond(502)
        cli = HttpMcpGatewayClient(cfg, max_retries=0)
        try:
            cli.call_tool("gh")
            raised = False
        except McpGatewayError as e:
            raised = e
        assert raised

    def test_construct_without_url_raises(self):
        with pytest.raises(McpGatewayError):
            HttpMcpGatewayClient(McpGatewayClientConfig(url=""))
