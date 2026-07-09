"""Tests for HttpAgentsGatewayClient: retries, structured errors, idempotency passthrough."""

import httpx
import pytest
import respx

from conductor.config import AgentsGatewayClientConfig
from conductor.clients.agents_gateway import (
    HttpAgentsGatewayClient,
    AgentsGatewayError,
    MockAgentsGatewayClient,
)
from conductor.clients.agents_gateway import TaskInfo


@pytest.fixture
def cfg():
    return AgentsGatewayClientConfig(
        url="http://agents.test",
        auth_mode="internal-only",
        internal_token="t0p",
        timeout_seconds=5.0,
    )


class TestStructuredErrors:
    def test_4xx_raises_structured_error_non_transient(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.get("/agents").respond(404, json={"detail": "not found"})
            client = HttpAgentsGatewayClient(cfg, max_retries=2)
            with pytest.raises(AgentsGatewayError) as ei:
                client.list_agents()
            err = ei.value
            assert err.status == 404
            assert err.method == "GET /agents"
            assert err.transient is False
            assert "not found" in err.body

    def test_5xx_raises_structured_error_transient(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.get("/agents").respond(503, json={"detail": "down"})
            client = HttpAgentsGatewayClient(cfg, max_retries=0)
            with pytest.raises(AgentsGatewayError) as ei:
                client.list_agents()
            err = ei.value
            assert err.status == 503
            assert err.transient is True

    def test_transport_error_raises_transient(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.get("/agents").mock(side_effect=httpx.ConnectError("conn refused"))
            client = HttpAgentsGatewayClient(cfg, max_retries=0)
            with pytest.raises(AgentsGatewayError) as ei:
                client.list_agents()
            assert ei.value.transient is True


class TestRetries:
    def test_500_then_200_succeeds(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            route = m.get("/agents")
            route.respond(500)
            route.respond(200, json=[{"id": "a1", "name": "Agent 1"}])
            client = HttpAgentsGatewayClient(cfg, max_retries=3)
            # Patch backoff to instant for tests
            client._backoff = lambda attempt: None
            agents = client.list_agents()
            assert len(agents) == 1
            assert agents[0].id == "a1"

    def test_4xx_not_retried(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            route = m.get("/agents")
            route.respond(400, json={"detail": "bad"})
            client = HttpAgentsGatewayClient(cfg, max_retries=5)
            client._backoff = lambda attempt: None
            with pytest.raises(AgentsGatewayError) as ei:
                client.list_agents()
            assert ei.value.status == 400
            # Should have been called exactly once
            assert route.call_count == 1

    def test_retries_exhausted(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            route = m.get("/agents")
            for _ in range(5):
                route.respond(503)
            client = HttpAgentsGatewayClient(cfg, max_retries=2)
            client._backoff = lambda attempt: None
            with pytest.raises(AgentsGatewayError) as ei:
                client.list_agents()
            assert ei.value.status == 503
            # called initial + 2 retries = 3 total
            assert route.call_count == 3

    def test_timeout_then_success(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            route = m.get("/agents")
            route.mock(side_effect=httpx.ConnectTimeout("slow"))
            route.respond(200, json=[{"id": "a1", "name": "Agent 1"}])
            client = HttpAgentsGatewayClient(cfg, max_retries=3)
            client._backoff = lambda attempt: None
            agents = client.list_agents()
            assert agents[0].id == "a1"


class TestIdempotencyPassthrough:
    def test_create_task_sends_idempotency_key(self, cfg):
        captured = {}

        def _record(request):
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"task": {"id": "gw-1", "agent_id": "ag", "status": "created"}})

        with respx.mock(base_url="http://agents.test") as m:
            m.post("/tasks").mock(side_effect=_record)
            client = HttpAgentsGatewayClient(cfg)
            client.create_task("ag", "hello", idempotency_key="o1:r1:t1:1")
            assert captured["body"]["idempotency_key"] == "o1:r1:t1:1"
            assert captured["body"]["agent_id"] == "ag"
            assert captured["body"]["input"] == "hello"

    def test_create_task_no_idempotency_key_omitted(self, cfg):
        captured = {}

        def _record(request):
            import json
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"task": {"id": "gw-1", "status": "created"}})

        with respx.mock(base_url="http://agents.test") as m:
            m.post("/tasks").mock(side_effect=_record)
            client = HttpAgentsGatewayClient(cfg)
            client.create_task("ag", "hello")
            assert "idempotency_key" not in captured["body"]

    def test_run_task_passes_through(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.post("/tasks/gw-1/run").respond(200, json={"task": {"id": "gw-1", "status": "running"}})
            client = HttpAgentsGatewayClient(cfg)
            t = client.run_task("gw-1")
            assert t.id == "gw-1"
            assert t.status == "running"

    def test_get_task_with_output(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.get("/tasks/gw-1").respond(200, json={"task": {
                "id": "gw-1", "status": "completed", "output": "all green"}})
            client = HttpAgentsGatewayClient(cfg)
            t = client.get_task("gw-1")
            assert t.status == "completed"
            assert t.output == "all green"

    def test_get_events(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.get("/tasks/gw-1/events").respond(200, json={"events": [
                {"id": "e1", "task_id": "gw-1", "event": "task.started", "created_at": "now"},
            ]})
            client = HttpAgentsGatewayClient(cfg)
            evts = client.get_events("gw-1")
            assert len(evts) == 1
            assert evts[0].event == "task.started"

    def test_get_artifacts(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.get("/tasks/gw-1/artifacts").respond(200, json={"artifacts": [
                {"id": "a1", "task_id": "gw-1", "name": "report.log", "size_bytes": 1024},
            ]})
            client = HttpAgentsGatewayClient(cfg)
            arts = client.get_artifacts("gw-1")
            assert arts[0].name == "report.log"
            assert arts[0].size_bytes == 1024


class TestMockIdempotencyKeys:
    """The MockAgentsGatewayClient now also collapses on idempotency keys, mirroring the gateway."""

    def test_mock_collapses_on_idempotency_key(self):
        mock = MockAgentsGatewayClient()
        mock.register_agent("a1", "A")
        t1 = mock.create_task("a1", "x", idempotency_key="k1")
        t2 = mock.create_task("a1", "x", idempotency_key="k1")
        assert t1.id == t2.id

    def test_mock_distinct_keys_distinct_tasks(self):
        mock = MockAgentsGatewayClient()
        mock.register_agent("a1", "A")
        t1 = mock.create_task("a1", "x", idempotency_key="k1")
        t2 = mock.create_task("a1", "x", idempotency_key="k2")
        assert t1.id != t2.id
