"""Agents Gateway HTTP client with mock support for offline testing.

Production HTTP client includes:
- authentication via X-Auth-Internal-Token header
- configurable timeout
- bounded exponential-backoff retries on connection errors and 5xx responses
  (4xx is not retried — caller errors should fail fast; 429 is retried)
- structured AgentsGatewayError with method, url, status, body
- idempotency-key passthrough so duplicate dispatches collapse on the gateway side
- harness profile catalog + availability
- harness task creation with full spec
- session capture/send/stop
- Composer interaction listing/reply/cancel
- enriched TaskInfo with runtime_status, blockers, interaction_pending, metadata
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
    runtime: str = ""        # runtime type ("stub", "harness_session", etc.)
    runtime_type: str = ""   # alias for runtime
    risk_level: str = "low"
    kind: str = "agent"      # "agent" | "harness"


@dataclass
class HarnessProfileInfo:
    name: str = ""
    harness: str = ""
    display_name: str = ""
    command: str = ""
    supports_slash_goal: bool = False
    goal_command: str = ""
    input_mode: str = ""
    completion_strategy: str = ""
    goal_strategy: str = "auto"
    description: str = ""
    default: bool = False


@dataclass
class HarnessAvailabilityInfo:
    profile: str = ""
    harness: str = ""
    configured: bool = False
    binary_present: bool = False
    credentials_present: bool = False
    runnable: bool = False
    command: str = ""
    error: str = ""


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
    metadata: dict = field(default_factory=dict)
    runtime_status: str | None = None
    blockers: list[str] = field(default_factory=list)
    interaction_pending: bool = False
    harness: dict = field(default_factory=dict)


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
    artifact_type: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class SessionInfo:
    id: str = ""
    agent_run_id: str = ""
    task_id: str = ""
    harness_profile: str = ""
    harness: str = ""
    status: str = "created"
    tmux_session: str = ""
    working_directory: str = ""
    started_at: str = ""
    ended_at: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class ComposerInteraction:
    id: str
    agent_run_id: str = ""
    task_id: str = ""
    session_id: str = ""
    type: str = "needs_reply"
    status: str = "pending"
    prompt_excerpt: str = ""
    full_context_ref: str = ""
    created_at: str = ""
    resolved_at: str | None = None
    composer_reply: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class VerificationInfo:
    id: str = ""
    agent_run_id: str = ""
    task_id: str = ""
    status: str = "pending"
    started_at: str = ""
    completed_at: str = ""
    commands: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class WorktreeInfo:
    id: str = ""
    task_id: str = ""
    agent_run_id: str = ""
    branch: str = ""
    base_branch: str = ""
    commit_sha: str = ""
    path: str = ""
    status: str = ""
    created_at: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class SessionCapture:
    session_id: str = ""
    status: str = ""
    capture: str = ""
    captured_at: str = ""
    lines: int = 0


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class BaseAgentsGatewayClient:
    """Interface for Agents Gateway operations."""

    def list_agents(self) -> list[AgentInfo]:
        raise NotImplementedError

    def list_harness_profiles(self) -> list[HarnessProfileInfo]:
        raise NotImplementedError

    def check_harness_availability(self, profile_name: str) -> HarnessAvailabilityInfo:
        raise NotImplementedError

    def create_task(self, agent_id: str, input_data: str, metadata: dict | None = None, idempotency_key: str = "") -> TaskInfo:
        raise NotImplementedError

    def create_harness_task(self, spec: dict, idempotency_key: str = "") -> TaskInfo:
        raise NotImplementedError

    def run_task(self, task_id: str) -> TaskInfo:
        raise NotImplementedError

    def get_task(self, task_id: str) -> TaskInfo:
        raise NotImplementedError

    def cancel_task(self, task_id: str) -> TaskInfo:
        raise NotImplementedError

    def get_events(self, task_id: str) -> list[TaskEvent]:
        raise NotImplementedError

    def get_artifacts(self, task_id: str) -> list[TaskArtifact]:
        raise NotImplementedError

    def get_session(self, session_id: str) -> SessionInfo:
        raise NotImplementedError

    def get_task_session(self, task_id: str) -> SessionInfo | None:
        raise NotImplementedError

    def capture_session(self, session_id: str, lines: int = 200) -> SessionCapture:
        raise NotImplementedError

    def send_to_session(self, session_id: str, text: str, submit: bool = True) -> dict:
        raise NotImplementedError

    def stop_session(self, session_id: str) -> dict:
        raise NotImplementedError

    def list_interactions(self, *, status: str = "", task_id: str = "") -> list[ComposerInteraction]:
        raise NotImplementedError

    def get_interaction(self, interaction_id: str) -> ComposerInteraction:
        raise NotImplementedError

    def reply_to_interaction(self, interaction_id: str, reply: str) -> dict:
        raise NotImplementedError

    def cancel_interaction(self, interaction_id: str) -> dict:
        raise NotImplementedError

    def get_verification(self, agent_run_id: str) -> VerificationInfo:
        raise NotImplementedError

    def get_task_worktree(self, task_id: str) -> WorktreeInfo | None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class MockAgentsGatewayClient(BaseAgentsGatewayClient):
    """Mock client that stores tasks in memory for testing."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentInfo] = {}
        self._harness_profiles: dict[str, HarnessProfileInfo] = {}
        self._tasks: dict[str, TaskInfo] = {}
        self._events: dict[str, list[TaskEvent]] = {}
        self._artifacts: dict[str, list[TaskArtifact]] = {}
        self._sessions: dict[str, SessionInfo] = {}
        self._task_sessions: dict[str, str] = {}  # task_id -> session_id
        self._interactions: dict[str, ComposerInteraction] = {}
        self._task_interactions: dict[str, list[str]] = {}  # task_id -> interaction_id list
        self._worktrees: dict[str, WorktreeInfo] = {}
        self._counter = 0
        self._seen_idempotency_keys: dict[str, str] = {}
        self._harness_availability: dict[str, HarnessAvailabilityInfo] = {}
        self._verifications: dict[str, VerificationInfo] = {}

    def _next_id(self) -> str:
        self._counter += 1
        return f"mock-gw-task-{self._counter}"

    def register_agent(self, id: str, name: str, runtime: str = "stub") -> None:
        self._agents[id] = AgentInfo(id=id, name=name, runtime=runtime, runtime_type=runtime)

    def register_harness_profile(self, name: str, display_name: str = "", runnable: bool = True) -> None:
        self._harness_profiles[name] = HarnessProfileInfo(
            name=name, display_name=display_name or name,
            goal_strategy="auto",
        )
        self._harness_availability[name] = HarnessAvailabilityInfo(
            profile=name, configured=True, binary_present=runnable,
            credentials_present=runnable, runnable=runnable, command="mock",
        )

    def list_agents(self) -> list[AgentInfo]:
        return list(self._agents.values())

    def list_harness_profiles(self) -> list[HarnessProfileInfo]:
        return list(self._harness_profiles.values())

    def check_harness_availability(self, profile_name: str) -> HarnessAvailabilityInfo:
        return self._harness_availability.get(
            profile_name,
            HarnessAvailabilityInfo(profile=profile_name, runnable=False),
        )

    def create_task(self, agent_id: str, input_data: str, metadata: dict | None = None, idempotency_key: str = "") -> TaskInfo:
        if idempotency_key and idempotency_key in self._seen_idempotency_keys:
            existing_id = self._seen_idempotency_keys[idempotency_key]
            return self._tasks[existing_id]
        if agent_id not in self._agents:
            agent = AgentInfo(id=agent_id, name=agent_id)
            self._agents[agent_id] = agent
        task = TaskInfo(id=self._next_id(), agent_id=agent_id, status="created",
                        input=input_data, metadata=metadata or {})
        self._tasks[task.id] = task
        self._events[task.id] = []
        self._artifacts[task.id] = []
        if idempotency_key:
            self._seen_idempotency_keys[idempotency_key] = task.id
        return task

    def create_harness_task(self, spec: dict, idempotency_key: str = "") -> TaskInfo:
        if idempotency_key and idempotency_key in self._seen_idempotency_keys:
            existing_id = self._seen_idempotency_keys[idempotency_key]
            return self._tasks[existing_id]
        profile = spec.get("execution", {}).get("harness_profile", "")
        spec_meta = spec.get("metadata", {}) or {}
        metadata = {
            "composer_task_id": spec.get("composer_task_id", ""),
            "objective_id": spec.get("objective_id", ""),
            "title": spec.get("title", ""),
            "runtime_type": "harness_session",
            "harness_profile": profile,
        }
        # Preserve all spec.metadata nested keys (composer_node_id, composer_plan_id, etc.)
        for k, v in spec_meta.items():
            metadata.setdefault(k, v)
        task = TaskInfo(id=self._next_id(), agent_id=profile or "harness_session",
                        status="created", input="", metadata=metadata)
        # Store full spec in metadata
        task.metadata["spec"] = spec
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
        task.status = "queued"
        self._tasks[task_id] = task
        return task

    def complete_task(self, task_id: str, output: str = "") -> TaskInfo:
        task = self._tasks[task_id]
        task.status = "completed"
        task.output = output
        task.runtime_status = "completed"
        self._tasks[task_id] = task
        return task

    def fail_task(self, task_id: str, error: str = "") -> TaskInfo:
        task = self._tasks[task_id]
        task.status = "failed"
        task.error = error
        task.runtime_status = "failed"
        self._tasks[task_id] = task
        return task

    def set_task_waiting(self, task_id: str) -> TaskInfo:
        task = self._tasks[task_id]
        task.status = "waiting"
        task.runtime_status = "waiting_for_reply"
        task.interaction_pending = True
        self._tasks[task_id] = task
        return task

    def set_task_blocked(self, task_id: str, blockers: list[str] | None = None) -> TaskInfo:
        task = self._tasks[task_id]
        task.status = "waiting"
        task.runtime_status = "blocked_external"
        task.blockers = blockers or ["blocked_external: missing binary or credential"]
        self._tasks[task_id] = task
        return task

    def cancel_task(self, task_id: str) -> TaskInfo:
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        task.status = "cancelled"
        task.runtime_status = "cancelled"
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

    # Session support

    def create_mock_session(self, task_id: str, profile: str = "opencode-deepseek") -> SessionInfo:
        session_id = f"mock-session-{self._counter}"
        session = SessionInfo(id=session_id, task_id=task_id, harness_profile=profile,
                             status="running", working_directory=f"/tmp/worktree-{session_id}")
        self._sessions[session_id] = session
        self._task_sessions[task_id] = session_id
        return session

    def get_session(self, session_id: str) -> SessionInfo:
        return self._sessions.get(session_id, SessionInfo(id=session_id, status="created"))

    def get_task_session(self, task_id: str) -> SessionInfo | None:
        session_id = self._task_sessions.get(task_id)
        if not session_id:
            return None
        return self._sessions.get(session_id)

    def capture_session(self, session_id: str, lines: int = 200) -> SessionCapture:
        return SessionCapture(session_id=session_id, status="running",
                              capture="", captured_at="", lines=0)

    def send_to_session(self, session_id: str, text: str, submit: bool = True) -> dict:
        return {"session_id": session_id, "status": "sent", "text_chars": len(text)}

    def stop_session(self, session_id: str) -> dict:
        if session_id in self._sessions:
            self._sessions[session_id].status = "cancelled"
        return {"session_id": session_id, "status": "cancelled"}

    # Interaction support

    def create_mock_interaction(self, task_id: str, prompt: str = "How should I handle this?",
                                interaction_type: str = "needs_reply") -> ComposerInteraction:
        interaction_id = f"int-{self._counter}"
        session = self._task_sessions.get(task_id)
        interaction = ComposerInteraction(
            id=interaction_id, task_id=task_id,
            session_id=session or "", type=interaction_type,
            status="pending", prompt_excerpt=prompt,
            created_at="",
        )
        self._interactions[interaction_id] = interaction
        self._task_interactions.setdefault(task_id, []).append(interaction_id)
        return interaction

    def list_interactions(self, *, status: str = "", task_id: str = "") -> list[ComposerInteraction]:
        result = list(self._interactions.values())
        if status:
            result = [i for i in result if i.status == status]
        if task_id:
            result = [i for i in result if i.task_id == task_id]
        return result

    def get_interaction(self, interaction_id: str) -> ComposerInteraction:
        return self._interactions.get(interaction_id, ComposerInteraction(id=interaction_id, status="not_found"))

    def reply_to_interaction(self, interaction_id: str, reply: str) -> dict:
        interaction = self._interactions.get(interaction_id)
        if interaction:
            interaction.status = "answered"
            interaction.composer_reply = reply
            # Clear pending flag on task
            task = self._tasks.get(interaction.task_id)
            if task:
                task.interaction_pending = False
                task.status = "running"
                task.runtime_status = "running"
        return {"interaction_id": interaction_id, "status": "answered"}

    def cancel_interaction(self, interaction_id: str) -> dict:
        interaction = self._interactions.get(interaction_id)
        if interaction:
            interaction.status = "cancelled"
        return {"interaction_id": interaction_id, "status": "cancelled"}

    # Verification

    def get_verification(self, agent_run_id: str) -> VerificationInfo:
        return self._verifications.get(agent_run_id, VerificationInfo(agent_run_id=agent_run_id, status="pending"))

    def set_verification(self, agent_run_id: str, status: str, commands: list[dict] | None = None) -> VerificationInfo:
        info = VerificationInfo(agent_run_id=agent_run_id, status=status, commands=commands or [])
        self._verifications[agent_run_id] = info
        return info

    # Worktree

    def set_task_worktree(self, task_id: str, branch: str = "",
                         commit_sha: str = "", status: str = "active") -> WorktreeInfo:
        wt_id = f"wt-{task_id}"
        wt = WorktreeInfo(id=wt_id, task_id=task_id, branch=branch or f"composer/branch-{task_id}",
                          base_branch="master", path=f"/tmp/{wt_id}", commit_sha=commit_sha, status=status)
        self._worktrees[wt_id] = wt
        return wt

    def get_task_worktree(self, task_id: str) -> WorktreeInfo | None:
        wt_id = f"wt-{task_id}"
        return self._worktrees.get(wt_id, WorktreeInfo(
            id=wt_id, task_id=task_id,
            branch=f"composer/branch-{task_id}",
            base_branch="master", path=f"/tmp/{wt_id}",
            status="active",
        ))

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
        """Perform a request with bounded exponential backoff retries."""
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

    # ── Agents ──────────────────────────────────────────────────────────

    def list_agents(self) -> list[AgentInfo]:
        r = self._request("GET", "/agents")
        self._raise_for_status(r, "GET /agents")
        agents_json = r.json()
        agents_list: list[dict] = []
        if isinstance(agents_json, dict):
            agents_list = agents_json.get("agents", agents_json.get("data", []))
        elif isinstance(agents_json, list):
            agents_list = agents_json
        result = []
        for a in agents_list:
            runtime_info = a.get("runtime", {})
            runtime_type = runtime_info.get("type", "") if isinstance(runtime_info, dict) else ""
            result.append(AgentInfo(
                id=a.get("id", ""), name=a.get("name", ""),
                description=a.get("description", ""), version=a.get("version", ""),
                runtime=runtime_type, runtime_type=runtime_type,
                risk_level=a.get("risk_level", "low"),
                kind=a.get("kind", "agent"),
            ))
        return result

    # ── Harness Profiles ─────────────────────────────────────────────────

    def list_harness_profiles(self) -> list[HarnessProfileInfo]:
        r = self._request("GET", "/harness-profiles")
        self._raise_for_status(r, "GET /harness-profiles")
        data = r.json()
        profiles = data.get("profiles", data.get("data", []))
        return [
            HarnessProfileInfo(
                name=p.get("name", ""),
                harness=p.get("harness", ""),
                display_name=p.get("display_name", p.get("name", "")),
                command=p.get("command", ""),
                supports_slash_goal=p.get("supports_slash_goal", False),
                goal_command=p.get("goal_command", ""),
                input_mode=p.get("input_mode", ""),
                completion_strategy=p.get("completion_strategy", ""),
                goal_strategy=p.get("goal_strategy", "auto"),
                description=p.get("description", ""),
                default=p.get("default", False),
            )
            for p in profiles
        ]

    def check_harness_availability(self, profile_name: str) -> HarnessAvailabilityInfo:
        r = self._request("GET", f"/harness-profiles/{profile_name}/availability")
        self._raise_for_status(r, f"GET /harness-profiles/{profile_name}/availability")
        data = r.json()
        return HarnessAvailabilityInfo(
            profile=data.get("profile", profile_name),
            harness=data.get("harness", ""),
            configured=data.get("configured", False),
            binary_present=data.get("binary_present", False),
            credentials_present=data.get("credentials_present", False),
            runnable=data.get("runnable", False),
            command=data.get("command", ""),
            error=data.get("error", ""),
        )

    # ── Tasks ──────────────────────────────────────────────────────────

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
        return TaskInfo(
            id=t["id"], agent_id=t.get("agent_id", agent_id),
            status=t.get("status", "created"), input=t.get("input", ""),
            created_at=t.get("created_at", ""), updated_at=t.get("updated_at", ""),
            metadata=t.get("metadata", {}),
            runtime_status=t.get("runtime_status"),
            blockers=t.get("blockers", []),
            interaction_pending=t.get("interaction_pending", False),
            harness=t.get("harness", {}),
        )

    def create_harness_task(self, spec: dict, idempotency_key: str = "") -> TaskInfo:
        payload = dict(spec)
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        r = self._request("POST", "/tasks", json_body=payload)
        self._raise_for_status(r, "POST /tasks")
        data = r.json()
        t = data.get("task", data)
        return TaskInfo(
            id=t["id"], agent_id=t.get("agent_id", ""),
            status=t.get("status", "created"), input=t.get("input", ""),
            created_at=t.get("created_at", ""), updated_at=t.get("updated_at", ""),
            metadata=t.get("metadata", {}),
            runtime_status=t.get("runtime_status"),
            blockers=t.get("blockers", []),
            interaction_pending=t.get("interaction_pending", False),
            harness=t.get("harness", {}),
        )

    def run_task(self, task_id: str) -> TaskInfo:
        r = self._request("POST", f"/tasks/{task_id}/run")
        self._raise_for_status(r, f"POST /tasks/{task_id}/run")
        data = r.json()
        t = data.get("task", data)
        return TaskInfo(
            id=t.get("id", task_id), agent_id=t.get("agent_id", ""),
            status=t.get("status", "queued"),
            created_at=t.get("created_at", ""),
            updated_at=t.get("updated_at", ""),
            metadata=t.get("metadata", {}),
            runtime_status=t.get("runtime_status"),
            blockers=t.get("blockers", []),
            interaction_pending=t.get("interaction_pending", False),
            harness=t.get("harness", {}),
        )

    def get_task(self, task_id: str) -> TaskInfo:
        r = self._request("GET", f"/tasks/{task_id}")
        self._raise_for_status(r, f"GET /tasks/{task_id}")
        data = r.json()
        t = data.get("task", data)
        return TaskInfo(
            id=t["id"], agent_id=t.get("agent_id", ""),
            status=t.get("status", ""), input=t.get("input", ""),
            output=t.get("output", ""), error=t.get("error", ""),
            created_at=t.get("created_at", ""), updated_at=t.get("updated_at", ""),
            metadata=t.get("metadata", {}),
            runtime_status=t.get("runtime_status"),
            blockers=t.get("blockers", []),
            interaction_pending=t.get("interaction_pending", False),
            harness=t.get("harness", {}),
        )

    def cancel_task(self, task_id: str) -> TaskInfo:
        r = self._request("POST", f"/tasks/{task_id}/cancel")
        self._raise_for_status(r, f"POST /tasks/{task_id}/cancel")
        data = r.json()
        t = data.get("task", data)
        return TaskInfo(
            id=t["id"], agent_id=t.get("agent_id", ""),
            status=t.get("status", "cancelled"),
            metadata=t.get("metadata", {}),
            runtime_status=t.get("runtime_status"),
            blockers=t.get("blockers", []),
        )

    def get_events(self, task_id: str) -> list[TaskEvent]:
        r = self._request("GET", f"/tasks/{task_id}/events")
        self._raise_for_status(r, f"GET /tasks/{task_id}/events")
        data = r.json()
        evts = data.get("events", data.get("data", []))
        return [TaskEvent(id=e["id"], task_id=e.get("task_id", task_id),
                         event=e.get("event", ""), data=e.get("data", {}),
                         created_at=e.get("created_at", "")) for e in evts]

    def get_artifacts(self, task_id: str) -> list[TaskArtifact]:
        r = self._request("GET", f"/tasks/{task_id}/artifacts")
        self._raise_for_status(r, f"GET /tasks/{task_id}/artifacts")
        data = r.json()
        arts = data.get("artifacts", data.get("data", []))
        return [TaskArtifact(id=a["id"], task_id=a.get("task_id", task_id),
                             name=a.get("name", ""), path=a.get("path", ""),
                             size_bytes=a.get("size_bytes", 0),
                             created_at=a.get("created_at", ""),
                             artifact_type=a.get("type", ""),
                             metadata=a.get("metadata", {})) for a in arts]

    # ── Sessions ───────────────────────────────────────────────────────

    def get_session(self, session_id: str) -> SessionInfo:
        r = self._request("GET", f"/sessions/{session_id}")
        self._raise_for_status(r, f"GET /sessions/{session_id}")
        data = r.json()
        return SessionInfo(
            id=data.get("id", session_id),
            agent_run_id=data.get("agent_run_id", ""),
            task_id=data.get("task_id", ""),
            harness_profile=data.get("harness_profile", ""),
            harness=data.get("harness", ""),
            status=data.get("status", "created"),
            tmux_session=data.get("tmux_session", ""),
            working_directory=data.get("working_directory", ""),
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at", ""),
            metadata=data.get("metadata", {}),
        )

    def get_task_session(self, task_id: str) -> SessionInfo | None:
        r = self._request("GET", f"/tasks/{task_id}/session")
        if r.status_code == 404:
            return None
        self._raise_for_status(r, f"GET /tasks/{task_id}/session")
        data = r.json()
        return SessionInfo(
            id=data.get("id", ""),
            agent_run_id=data.get("agent_run_id", ""),
            task_id=data.get("task_id", task_id),
            harness_profile=data.get("harness_profile", ""),
            harness=data.get("harness", ""),
            status=data.get("status", ""),
            tmux_session=data.get("tmux_session", ""),
            working_directory=data.get("working_directory", ""),
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at", ""),
            metadata=data.get("metadata", {}),
        )

    def capture_session(self, session_id: str, lines: int = 200) -> SessionCapture:
        r = self._request("GET", f"/sessions/{session_id}/capture?lines={lines}")
        self._raise_for_status(r, f"GET /sessions/{session_id}/capture")
        data = r.json()
        return SessionCapture(
            session_id=data.get("session_id", session_id),
            status=data.get("status", ""),
            capture=data.get("capture", ""),
            captured_at=data.get("captured_at", ""),
            lines=data.get("lines", 0),
        )

    def send_to_session(self, session_id: str, text: str, submit: bool = True) -> dict:
        r = self._request("POST", f"/sessions/{session_id}/send",
                          json_body={"text": text, "submit": submit})
        self._raise_for_status(r, f"POST /sessions/{session_id}/send")
        return r.json()

    def stop_session(self, session_id: str) -> dict:
        r = self._request("POST", f"/sessions/{session_id}/stop")
        self._raise_for_status(r, f"POST /sessions/{session_id}/stop")
        return r.json()

    # ── Interactions ───────────────────────────────────────────────────

    def list_interactions(self, *, status: str = "", task_id: str = "") -> list[ComposerInteraction]:
        params: list[str] = []
        if status:
            params.append(f"status={status}")
        if task_id:
            params.append(f"task_id={task_id}")
        query = "?" + "&".join(params) if params else ""
        r = self._request("GET", f"/interactions{query}")
        self._raise_for_status(r, "GET /interactions")
        data = r.json()
        interactions = data.get("interactions", data.get("data", []))
        return [
            ComposerInteraction(
                id=i.get("id", ""), agent_run_id=i.get("agent_run_id", ""),
                task_id=i.get("task_id", ""), session_id=i.get("session_id", ""),
                type=i.get("type", "needs_reply"), status=i.get("status", "pending"),
                prompt_excerpt=i.get("prompt_excerpt", ""),
                full_context_ref=i.get("full_context_ref", ""),
                created_at=i.get("created_at", ""),
                resolved_at=i.get("resolved_at"),
                composer_reply=i.get("composer_reply", ""),
                metadata=i.get("metadata", {}),
            )
            for i in interactions
        ]

    def get_interaction(self, interaction_id: str) -> ComposerInteraction:
        r = self._request("GET", f"/interactions/{interaction_id}")
        self._raise_for_status(r, f"GET /interactions/{interaction_id}")
        i = r.json()
        return ComposerInteraction(
            id=i.get("id", interaction_id),
            agent_run_id=i.get("agent_run_id", ""),
            task_id=i.get("task_id", ""),
            session_id=i.get("session_id", ""),
            type=i.get("type", "needs_reply"),
            status=i.get("status", "pending"),
            prompt_excerpt=i.get("prompt_excerpt", ""),
            full_context_ref=i.get("full_context_ref", ""),
            created_at=i.get("created_at", ""),
            resolved_at=i.get("resolved_at"),
            composer_reply=i.get("composer_reply", ""),
            metadata=i.get("metadata", {}),
        )

    def reply_to_interaction(self, interaction_id: str, reply: str) -> dict:
        r = self._request("POST", f"/interactions/{interaction_id}/reply",
                          json_body={"reply": reply})
        self._raise_for_status(r, f"POST /interactions/{interaction_id}/reply")
        return r.json()

    def cancel_interaction(self, interaction_id: str) -> dict:
        r = self._request("POST", f"/interactions/{interaction_id}/cancel")
        self._raise_for_status(r, f"POST /interactions/{interaction_id}/cancel")
        return r.json()

    # ── Verification ───────────────────────────────────────────────────

    def get_verification(self, agent_run_id: str) -> VerificationInfo:
        r = self._request("GET", f"/agent-runs/{agent_run_id}/verification")
        self._raise_for_status(r, f"GET /agent-runs/{agent_run_id}/verification")
        data = r.json()
        return VerificationInfo(
            id=data.get("id", ""), agent_run_id=data.get("agent_run_id", agent_run_id),
            task_id=data.get("task_id", ""),
            status=data.get("status", "pending"),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            commands=data.get("commands", []),
            metadata=data.get("metadata", {}),
        )

    # ── Worktrees ───────────────────────────────────────────────────────

    def get_task_worktree(self, task_id: str) -> WorktreeInfo | None:
        r = self._request("GET", f"/tasks/{task_id}/worktree")
        if r.status_code == 404:
            return None
        self._raise_for_status(r, f"GET /tasks/{task_id}/worktree")
        data = r.json()
        return WorktreeInfo(
            id=data.get("id", ""),
            task_id=data.get("task_id", task_id),
            agent_run_id=data.get("agent_run_id", ""),
            branch=data.get("branch", ""),
            base_branch=data.get("base_branch", ""),
            path=data.get("path", ""),
            status=data.get("status", ""),
            created_at=data.get("created_at", ""),
            metadata=data.get("metadata", {}),
        )

    def close(self) -> None:
        self._client.close()
