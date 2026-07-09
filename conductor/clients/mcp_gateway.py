"""MCP Gateway downstream client.

Conductor treats MCP Gateway as one of several downstream capability
providers (alongside Agents Gateway, Skills Gateway, and wiki-mcp). This
module mirrors the structure of conductor/clients/agents_gateway.py:

- BaseMcpGatewayClient — interface
- MockMcpGatewayClient  — in-memory mock for offline dev/tests
- HttpMcpGatewayClient  — real HTTP client with auth, timeouts, retries
                          (bounded exponential backoff on 5xx / transport
                          errors), and redacted logs

MCP Gateway API surface assumed:
  GET  /health        → {"status":"ok","service":"..."}
  GET  /version       → {"service":"...","version":"..."}
  POST /tools/list    → {"tools": [{name, description, input_schema}, ...]}
  POST /tools/call    → {"name": str, "arguments": dict} → {"result": ...}

This surface matches the spec's required method set
(health / version / list_tools / call_tool). If the real MCP Gateway
exposes a different shape, the HTTP client can be adjusted without
changing callers — the interface is abstract enough to support both.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from conductor.config import McpGatewayClientConfig
from conductor.logging import get_logger

logger = get_logger()


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class McpGatewayError(Exception):
    """Structured errors from the MCP Gateway client.

    Carries method/url/status/body so event emission can include detail
    without re-parsing the exception string.
    """

    def __init__(self, message: str, *, method: str = "", url: str = "",
                 status: int | None = None, body: str = "",
                 transient: bool = False) -> None:
        super().__init__(message)
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        self.transient = transient

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.method:
            parts.append(f"method={self.method}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.url:
            # Redact query string and any obvious token-looking query params.
            safe = re.sub(r"([?&][^=&]*token[^=&]*=)[^&]+", r"\1<redacted>", self.url)
            safe = re.sub(r"([?&][^=&]*secret[^=&]*=)[^&]+", r"\1<redacted>", safe)
            parts.append(f"url={safe}")
        return " ".join(parts)


@dataclass
class McpTool:
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


class BaseMcpGatewayClient:
    """Interface for MCP Gateway operations."""

    def health(self) -> dict:
        raise NotImplementedError

    def version(self) -> dict:
        raise NotImplementedError

    def list_tools(self) -> list[McpTool]:
        raise NotImplementedError

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        raise NotImplementedError


class MockMcpGatewayClient(BaseMcpGatewayClient):
    """In-memory mock MCP gateway. Useful for offline tests and dev envs."""

    def __init__(self) -> None:
        self._tools: dict[str, McpTool] = {
            "github.search": McpTool(
                name="github.search",
                description="Search GitHub repositories and code.",
                input_schema={"query": {"type": "string", "required": True}},
            ),
            "drive.read": McpTool(
                name="drive.read",
                description="Read a file from Google Drive.",
                input_schema={"file_id": {"type": "string", "required": True}},
            ),
        }
        self.calls: list[tuple[str, dict | None]] = []
        self._healthy = True
        self._version_str = "1.0.0-test"

    def set_healthy(self, healthy: bool) -> None:
        self._healthy = healthy

    def health(self) -> dict:
        return {"status": "ok" if self._healthy else "down",
                "service": "mock-mcp-gateway"}

    def version(self) -> dict:
        return {"service": "mock-mcp-gateway", "version": self._version_str}

    def list_tools(self) -> list[McpTool]:
        return list(self._tools.values())

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append((name, arguments))
        if name not in self._tools:
            raise McpGatewayError(f"unknown tool: {name}",
                                  method="POST /tools/call",
                                  status=404)
        return {
            "tool": name,
            "result": f"mock Ok for {name}",
            "input": arguments or {},
        }


class HttpMcpGatewayClient(BaseMcpGatewayClient):
    """Real HTTP client for MCP Gateway with retries and structured errors."""

    def __init__(self, config: McpGatewayClientConfig, max_retries: int = 2) -> None:
        self.config = config
        base = config.url.rstrip("/") if config.url else ""
        if not base:
            raise McpGatewayError("MCP Gateway URL not configured")
        self._client = httpx.Client(base_url=base, timeout=config.timeout_seconds)
        self._auth_header: dict[str, str] = {}
        if config.auth_mode == "internal-only" and config.internal_token:
            self._auth_header["X-Auth-Internal-Token"] = config.internal_token
        self._max_retries = max(0, max_retries)

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
        attempt = 0
        url = path
        while True:
            attempt += 1
            try:
                r = self._client.request(method, path, json=json_body,
                                         headers=self._auth_header)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt > self._max_retries:
                    logger.warning("mcp_gateway_transport_error method=%s url=%s attempt=%s", method, url, attempt)
                    raise McpGatewayError(f"transport error: {e}", method=method, url=url, transient=True) from e
                self._backoff(attempt)
                continue

            if r.status_code in RETRYABLE_STATUS and attempt <= self._max_retries:
                logger.info("mcp_gateway_retry method=%s url=%s status=%s attempt=%s",
                            method, url, r.status_code, attempt)
                self._backoff(attempt)
                continue
            return r

    def _backoff(self, attempt: int) -> None:
        base = min(2 ** (attempt - 1), 4.0)
        time.sleep(random.uniform(0, base))

    def _raise_for_status(self, r: httpx.Response, method: str) -> None:
        if r.is_success:
            return
        body = r.text[:500] if r.text else ""
        raise McpGatewayError(
            f"mcp gateway returned {r.status_code}",
            method=method, url=str(r.request.url),
            status=r.status_code, body=body,
            transient=r.status_code in RETRYABLE_STATUS,
        )

    def health(self) -> dict:
        r = self._request("GET", "/health")
        self._raise_for_status(r, "GET /health")
        return r.json()

    def version(self) -> dict:
        r = self._request("GET", "/version")
        self._raise_for_status(r, "GET /version")
        return r.json()

    def list_tools(self) -> list[McpTool]:
        r = self._request("POST", "/tools/list")
        self._raise_for_status(r, "POST /tools/list")
        data = r.json()
        tools_list = data.get("tools", data if isinstance(data, list) else [])
        return [
            McpTool(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("input_schema", {}) or {},
            )
            for t in tools_list
        ]

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        payload = {"name": name, "arguments": arguments or {}}
        r = self._request("POST", "/tools/call", json_body=payload)
        self._raise_for_status(r, "POST /tools/call")
        return r.json()

    def close(self) -> None:
        self._client.close()
