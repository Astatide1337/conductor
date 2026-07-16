"""wiki-mcp downstream client.

Conductor treats wiki-mcp as one of several downstream capability
providers (alongside Agents Gateway, Skills Gateway, and the MCP
Gateway). The wiki-mcp downstream owns durable project memory:
prior decisions, architecture notes, and project-wide conventions
that should be available to planning and report generation.

Two client implementations:

- BaseWikiMcpClient — interface used by Composer
- HttpWikiMcpClient  — real HTTP client (auth, timeouts, redacted logs)
- NullWikiMcpClient — no-op client (debugging, dev-no-auth mode)

The API surface is deliberately small:

  GET  /health                → {"status":"ok","service":"wiki-mcp"}
  GET  /context/{objective_id}→ {"context": {...}}   # durable memory
  POST /context/{objective_id}→ {"context": {...}}   # append-only

The interface method ``read_context`` is the only one Composer needs
from the planning-side; the writers (``append_context``) are exposed
for the report-finalization flow.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from conductor.config import WikiMcpClientConfig
from conductor.logging import get_logger

logger = get_logger()

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class WikiMcpError(Exception):
    def __init__(self, message: str, *, method: str = "", url: str = "",
                 status: int | None = None, body: str = "",
                 transient: bool = False) -> None:
        super().__init__(message)
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        self.transient = transient


@dataclass
class WikiContext:
    """Durable project-wide memory blob returned by wiki-mcp.

    ``context`` is the raw dict — an opaque JSON object — so callers
    (Composer planner) can pass it through verbatim into the prompt
    context package without coupling to wiki-mcp's schema.
    """
    context: dict
    service: str = "wiki-mcp"


class BaseWikiMcpClient:
    """Interface that Composer relies on for wiki-mcp access."""

    def read_context(self, objective_id: str) -> dict | None:
        raise NotImplementedError

    def append_context(self, objective_id: str, payload: dict) -> dict | None:
        raise NotImplementedError

    def health(self) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass


class NullWikiMcpClient(BaseWikiMcpClient):
    """No-op client used when wiki-mcp is unconfigured.

    Always returns ``None`` so the Composer planner skips memory
    injection rather than failing the whole objective.
    """

    def read_context(self, objective_id: str) -> dict | None:
        return None

    def append_context(self, objective_id: str, payload: dict) -> dict | None:
        return None

    def health(self) -> dict:
        return {"status": "disabled", "service": "wiki-mcp-null"}


class HttpWikiMcpClient(BaseWikiMcpClient):
    """Real HTTP client for wiki-mcp.

    Honors ``WikiMcpClientConfig.url`` — empty URL yields a null client
    so production wiring is a one-line check, not a try/except at every
    call site.

    Auth modes mirror Agents Gateway / Skills Gateway:
      * ``dev-none`` (default) — no Authorization header
      * ``internal_token``      — ``X-Internal-Token: <token>`` header
    """

    def __init__(self, cfg: WikiMcpClientConfig, *, max_retries: int = 3) -> None:
        if not cfg.url:
            raise ValueError("HttpWikiMcpClient requires a non-empty url")
        self._cfg = cfg
        self._url = cfg.url.rstrip("/")
        self._max_retries = max(0, int(max_retries))
        self._client = httpx.Client(timeout=cfg.timeout_seconds)
        self._auth_headers = self._build_auth_headers(cfg)

    @staticmethod
    def _build_auth_headers(cfg: WikiMcpClientConfig) -> dict[str, str]:
        if cfg.auth_mode == "internal_token" and cfg.internal_token:
            return {"X-Internal-Token": cfg.internal_token}
        return {}

    def _full(self, path: str) -> str:
        return f"{self._url}{path}"

    def _request(self, method: str, path: str, *,
                 json_body: dict | None = None) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                r = self._client.request(
                    method, self._full(path),
                    json=json_body, headers=self._auth_headers)
                if r.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                    time.sleep(min(2 ** attempt, 10) * 0.1)
                    continue
                return r
            except (httpx.TransportError, httpx.RemoteProtocolError) as e:
                last_exc = e
                if attempt < self._max_retries:
                    time.sleep(min(2 ** attempt, 10) * 0.1)
                    continue
                raise WikiMcpError(
                    f"Transport error during {method} {path}: {e}",
                    method=method, url=self._full(path),
                    transient=True) from e
        # Unreachable in practice — loop returns or raises.
        raise WikiMcpError(
            f"Exhausted retries during {method} {path}",
            method=method, url=self._full(path), transient=True)

    def read_context(self, objective_id: str) -> dict | None:
        try:
            r = self._request("GET", f"/context/{objective_id}")
        except WikiMcpError as exc:
            logger.warning("wiki-mcp read_context failed: %s", exc)
            return None
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            logger.warning(
                "wiki-mcp read_context returned %s body=%s",
                r.status_code, r.text[:200])
            return None
        data = r.json()
        return data.get("context") if isinstance(data, dict) else data

    def append_context(self, objective_id: str, payload: dict) -> dict | None:
        try:
            r = self._request(
                "POST", f"/context/{objective_id}",
                json_body={"context": payload})
        except WikiMcpError as exc:
            logger.warning("wiki-mcp append_context failed: %s", exc)
            return None
        if r.status_code >= 400:
            logger.warning(
                "wiki-mcp append_context returned %s body=%s",
                r.status_code, r.text[:200])
            return None
        return r.json() if r.content else None

    def health(self) -> dict:
        try:
            r = self._request("GET", "/health")
            return r.json() if r.status_code < 400 else {}
        except (WikiMcpError, Exception) as exc:
            return {"status": "error", "error": str(exc)}

    def close(self) -> None:
        self._client.close()


def build_wiki_mcp_client(cfg: WikiMcpClientConfig) -> BaseWikiMcpClient:
    """Factory: return a real client when configured, else Null.

    Used by the server bootstrap to wire the configured wiki-mcp client
    into the Composer service — never returns ``None``, so the planner
    has a stable typed interface regardless of configuration state.
    """
    if not cfg or not cfg.url:
        return NullWikiMcpClient()
    try:
        return HttpWikiMcpClient(cfg)
    except Exception as exc:
        logger.warning(
            "Failed to build HttpWikiMcpClient (falling back to null): %s",
            exc)
        return NullWikiMcpClient()


__all__ = [
    "WikiMcpError",
    "WikiContext",
    "BaseWikiMcpClient",
    "NullWikiMcpClient",
    "HttpWikiMcpClient",
    "build_wiki_mcp_client",
]
