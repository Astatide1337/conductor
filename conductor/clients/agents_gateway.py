"""Agents Gateway HTTP client with mock support for offline testing.

Production HTTP client includes:
- authentication via X-Auth-Internal-Token header
- configurable timeout
- bounded exponential-backoff retries on connection errors and 5xx responses
  (4xx is not retried — caller errors should fail fast; 429 is retried)
- structured AgentsGatewayError with method, url, status, body
- idempotency-key passthrough so duplicate dispatches collapse on the gateway side
"""

import random
import time
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock

import httpx

from conductor.config import AgentsGatewayClientConfig
from conductor.logging import get_logger

logger = get_logger()


class AgentsGatewayError(Exception):
    """Structured errors from the Agents Gateway client.

    Carries enough context to emit a useful event and decide whether to retry.
    """

    def __init__(self, message: str, *, method: str = "", url: str = "",
                 status: int | None = None, body: str = "", transient: bool = False) -> None:
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
            parts.append(f"url={self.url}")
        return " ".join(parts)


@dataclass
class AgentInfo:
    id: str
    name: str
    description: str = ""
    version: str = ""
    runtime: str = ""
    risk_level: str = "low"


@dataclass
class TaskInfo:
    id: str
    agent_id: str
    status: str = "created"
    input: str = ""
    output: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class TaskEvent:
    id: str
    task_id: str
    event: str
    data: dict = field(default_factory=dict)
    created_at: str = ""


@dataclass
class TaskArtifact:
    id: str
    task_id: str
    name: str
    path: str = ""
    size_bytes: int = 0
    created_at: str = ""


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class BaseAgentsGatewayClient:
    """Interface for Agents Gateway operations."""

    def list_agents(self) -> list[AgentInfo]:
        raise NotImplementedError

    def create_task(self, agent_id: str, input_data: str, metadata: dict | None = None, idempotency_key: str = "") -> TaskInfo:
        raise NotImplementedError

    def run_task(self, task_id: str) -> TaskInfo:
        raise NotImplementedError

    def get_task(self, task_id: str) -> TaskInfo:
        raise NotImplementedError

    def get_events(self, task_id: str) -> list[TaskEvent]:
        raise NotImplementedError

    def get_artifacts(self, task_id: str) -> list[TaskArtifact]:
        raise NotImplementedError


class MockAgentsGatewayClient(BaseAgentsGatewayClient):
    """Mock client that stores tasks in memory for testing."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentInfo] = {}
        self._tasks: dict[str, TaskInfo] = {}
        self._events: dict[str, list[TaskEvent]] = {}
        self._artifacts: dict[str, list[TaskArtifact]] = {}
        self._counter = 0
        self._seen_idempotency_keys: dict[str, str] = {}

    def _next_id(self) -> str:
        self._counter += 1
        return f"mock-gw-task-{self._counter}"

    def register_agent(self, id: str, name: str, runtime: str = "stub") -> None:
        self._agents[id] = AgentInfo(id=id, name=name, runtime=runtime)

    def list_agents(self) -> list[AgentInfo]:
        return list(self._agents.values())

    def create_task(self, agent_id: str, input_data: str, metadata: dict | None = None, idempotency_key: str = "") -> TaskInfo:
        if idempotency_key and idempotency_key in self._seen_idempotency_keys:
            existing_id = self._seen_idempotency_keys[idempotency_key]
            return self._tasks[existing_id]
        if agent_id not in self._agents:
            agent = AgentInfo(id=agent_id, name=agent_id)
            self._agents[agent_id] = agent
        task = TaskInfo(id=self._next_id(), agent_id=agent_id, status="created", input=input_data)
        self._tasks[task.id] = task
        self._events[task.id] = []
        self._artifacts[task.id] = []
        if idempotency_key:
            self._seen_idempotency_keys[idempotency_key] = task.id
        return task

    def run_task(self, task_id: str) -> TaskInfo:
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        task.status = "running"
        self._tasks[task_id] = task
        return task

    def complete_task(self, task_id: str, output: str = "") -> TaskInfo:
        task = self._tasks[task_id]
        task.status = "completed"
        task.output = output
        self._tasks[task_id] = task
        return task

    def fail_task(self, task_id: str, error: str = "") -> TaskInfo:
        task = self._tasks[task_id]
        task.status = "failed"
        task.error = error
        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> TaskInfo:
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        return task

    def get_events(self, task_id: str) -> list[TaskEvent]:
        return self._events.get(task_id, [])

    def get_artifacts(self, task_id: str) -> list[TaskArtifact]:
        return self._artifacts.get(task_id, [])

    def add_event(self, task_id: str, event_type: str, data: dict | None = None) -> None:
        evt = TaskEvent(id=f"evt-{len(self._events.get(task_id, []))}", task_id=task_id, event=event_type, data=data or {}, created_at="now")
        self._events.setdefault(task_id, []).append(evt)

    def add_artifact(self, task_id: str, name: str, path: str = "", size: int = 0) -> None:
        art = TaskArtifact(id=f"art-{len(self._artifacts.get(task_id, []))}", task_id=task_id, name=name, path=path, size_bytes=size, created_at="now")
        self._artifacts.setdefault(task_id, []).append(art)


class HttpAgentsGatewayClient(BaseAgentsGatewayClient):
    """Real HTTP client against Agents Gateway with retries and structured errors."""

    def __init__(self, config: AgentsGatewayClientConfig, max_retries: int = 3) -> None:
        self.config = config
        self._client = httpx.Client(base_url=config.url.rstrip("/"), timeout=config.timeout_seconds)
        self._auth_header: dict[str, str] = {}
        if config.auth_mode == "internal-only" and config.internal_token:
            self._auth_header["X-Auth-Internal-Token"] = config.internal_token
        self._max_retries = max(0, max_retries)

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
        """Perform a request with bounded exponential backoff retries.

        Retries on:
          - httpx.TransportError / httpx.TimeoutException (connection-level)
          - HTTP 429 and 5xx (transient server errors)
        Does NOT retry on:
          - HTTP 4xx (other than 429) — caller errors fail fast
          - HTTP 2xx — success returns immediately
        """
        url = path
        attempt = 0
        last_exc: Exception | None = None
        while True:
            attempt += 1
            try:
                r = self._client.request(method, path, json=json_body, headers=self._auth_header)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt > self._max_retries:
                    logger.warning("agents_gateway_transport_error method=%s url=%s attempt=%s", method, url, attempt)
                    raise AgentsGatewayError(
                        f"transport error: {e}", method=method, url=url, transient=True
                    ) from e
                self._backoff(attempt)
                continue

            if r.status_code in RETRYABLE_STATUS and attempt <= self._max_retries:
                logger.info("agents_gateway_retry method=%s url=%s status=%s attempt=%s", method, url, r.status_code, attempt)
                self._backoff(attempt)
                continue

            return r

    def _backoff(self, attempt: int) -> None:
        # Exponential backoff with full jitter: cap at 2^(attempt-1) seconds, max 8s.
        base = min(2 ** (attempt - 1), 8.0)
        delay = random.uniform(0, base)
        time.sleep(delay)

    def _raise_for_status(self, r: httpx.Response, method: str) -> None:
        if r.is_success:
            return
        body = r.text[:500] if r.text else ""
        raise AgentsGatewayError(
            f"agents gateway returned {r.status_code}",
            method=method, url=str(r.request.url),
            status=r.status_code, body=body,
            transient=r.status_code in RETRYABLE_STATUS,
        )

    def list_agents(self) -> list[AgentInfo]:
        r = self._request("GET", "/agents")
        self._raise_for_status(r, "GET /agents")
        agents_json = r.json()
        if isinstance(agents_json, dict):
            agents_list = agents_json.get("agents", agents_json.get("data", []))
        else:
            agents_list = agents_json
        return [AgentInfo(id=a.get("id",""), name=a.get("name",""), description=a.get("description",""), version=a.get("version",""), runtime=a.get("runtime_type",""), risk_level=a.get("risk_level","low")) for a in agents_list]

    def create_task(self, agent_id: str, input_data: str, metadata: dict | None = None, idempotency_key: str = "") -> TaskInfo:
        payload = {"agent_id": agent_id, "input": input_data}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if metadata:
            payload["metadata"] = metadata
        r = self._request("POST", "/tasks", json_body=payload)
        self._raise_for_status(r, "POST /tasks")
        data = r.json()
        t = data.get("task", data)
        return TaskInfo(id=t["id"], agent_id=t.get("agent_id", agent_id), status=t.get("status","created"), input=t.get("input",""), created_at=t.get("created_at",""), updated_at=t.get("updated_at",""))

    def run_task(self, task_id: str) -> TaskInfo:
        r = self._request("POST", f"/tasks/{task_id}/run")
        self._raise_for_status(r, f"POST /tasks/{task_id}/run")
        data = r.json()
        t = data.get("task", data)
        return TaskInfo(id=task_id, agent_id=t.get("agent_id", ""), status=t.get("status", "running"),
                        output=t.get("output", ""), created_at=t.get("created_at", ""), updated_at=t.get("updated_at", ""))

    def get_task(self, task_id: str) -> TaskInfo:
        r = self._request("GET", f"/tasks/{task_id}")
        self._raise_for_status(r, f"GET /tasks/{task_id}")
        data = r.json()
        t = data.get("task", data)
        return TaskInfo(id=t["id"], agent_id=t.get("agent_id",""), status=t.get("status",""), input=t.get("input",""), output=t.get("output",""), error=t.get("error",""), created_at=t.get("created_at",""), updated_at=t.get("updated_at",""))

    def get_events(self, task_id: str) -> list[TaskEvent]:
        r = self._request("GET", f"/tasks/{task_id}/events")
        self._raise_for_status(r, f"GET /tasks/{task_id}/events")
        data = r.json()
        evts = data.get("events", data.get("data", []))
        return [TaskEvent(id=e["id"], task_id=e.get("task_id", task_id), event=e.get("event",""), data=e.get("data",{}), created_at=e.get("created_at","")) for e in evts]

    def get_artifacts(self, task_id: str) -> list[TaskArtifact]:
        r = self._request("GET", f"/tasks/{task_id}/artifacts")
        self._raise_for_status(r, f"GET /tasks/{task_id}/artifacts")
        data = r.json()
        arts = data.get("artifacts", data.get("data", []))
        return [TaskArtifact(id=a["id"], task_id=a.get("task_id", task_id), name=a.get("name",""), path=a.get("path",""), size_bytes=a.get("size_bytes",0), created_at=a.get("created_at","")) for a in arts]

    def close(self) -> None:
        self._client.close()