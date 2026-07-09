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


class TestCreateTaskReturnsExternalId:
    """The HTTP client must surface the gateway-assigned task id, not the
    idempotency key, so reconciliation can find the task on the gateway side.
    """

    def test_create_task_returns_gateway_id_present_in_response(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.post("/tasks").respond(
                201,
                json={"task": {"id": "gw-901", "agent_id": "code-validator",
                                "status": "created", "input": "go"}},
            )
            client = HttpAgentsGatewayClient(cfg)
            t = client.create_task(
                agent_id="code-validator",
                input_data="go",
                idempotency_key="idem-1",
            )
            assert t.id == "gw-901", "external task id must come from the gateway response"
            assert t.agent_id == "code-validator"
            assert t.status == "created"

    def test_create_task_runs_through_201_status(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.post("/tasks").respond(
                201,
                json={"id": "gw-902", "status": "created"},
            )
            client = HttpAgentsGatewayClient(cfg)
            t = client.create_task(agent_id="cv", input_data="x")
            assert t.id == "gw-902"


class TestRunTaskStatusMapping:
    """run_task must surface the gateway's status string (typically 'running')
    rather than assume anything."""

    def test_run_task_returned_status_reflects_gateway(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.post("/tasks/gw-9/run").respond(
                202,
                json={"task": {"id": "gw-9", "status": "running"}},
            )
            client = HttpAgentsGatewayClient(cfg)
            t = client.run_task("gw-9")
            # 202 is the typical gateway response for an accepted run; status must map through.
            assert t.status == "running"

    def test_run_task_returns_202_running(self, cfg):
        """Spec-required: 'run_task -> 202'."""
        with respx.mock(base_url="http://agents.test") as m:
            m.post("/tasks/gw-x/run").respond(202, json={"status": "running"})
            client = HttpAgentsGatewayClient(cfg)
            t = client.run_task("gw-x")
            assert t.status == "running"


class TestGetTaskStatusTransitions:
    """get_task must faithfully map both 'running' and 'completed' states
    back to client callers. This is what reconcile loops rely on."""

    def test_get_task_running(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.get("/tasks/gw-1").respond(
                200,
                json={"task": {"id": "gw-1", "status": "running"}},
            )
            client = HttpAgentsGatewayClient(cfg)
            t = client.get_task("gw-1")
            assert t.status == "running"
            assert t.id == "gw-1"

    def test_get_task_completed(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.get("/tasks/gw-2").respond(
                200,
                json={"task": {"id": "gw-2", "status": "completed", "output": "done"}},
            )
            client = HttpAgentsGatewayClient(cfg)
            t = client.get_task("gw-2")
            assert t.status == "completed"
            assert t.output == "done"


class TestGatewayIdempotencyPreservedAcrossRepeats:
    """Re-dispatch with the same idempotency key must reuse the gateway task
    rather than creating a new one. This is what makes dispatch safe to retry
    after transient HTTP errors."""

    def test_http_create_task_then_repeat_returns_same_id(self, cfg):
        with respx.mock(base_url="http://agents.test") as m:
            m.post("/tasks").respond(
                201,
                json={"task": {"id": "gw-i1", "status": "created"}},
            )
            client = HttpAgentsGatewayClient(cfg)
            t1 = client.create_task(
                agent_id="cv", input_data="x", idempotency_key="idem-X")
            t2 = client.create_task(
                agent_id="cv", input_data="x", idempotency_key="idem-X")
            # The HTTP client isn't an idempotency cache; it forwards the same
            # key to the gateway and trusts the gateway to collapse. The point
            # is just that the *idempotency key itself* is preserved exactly
            # across repeated calls so the gateway sees the same key both times.
            assert t1.id == "gw-i1"
            assert t2.id == "gw-i1"
            # Verify both requests actually carried the same idempotency key.
            requests = [r for r in m.calls if r.request.url.path == "/tasks"]
            assert len(requests) == 2
            keys = []
            for r in requests:
                import json as _json
                body = r.request.content or b""
                try:
                    keys.append(_json.loads(body).get("idempotency_key"))
                except Exception:
                    keys.append(None)
            assert keys == ["idem-X", "idem-X"]


class TestFullOrchestrationFlowWithMockedHttp:
    """End-to-end: objective -> task -> dispatch -> external task created ->
    reconcile -> completed -> artifacts stored.

    Uses respx to mock the gateway so we never leave the test process.
    Exercises the *real* dispatch.py and storage against the *real* HTTP client.
    """

    def test_full_dispatch_and_reconcile_flow(self, cfg):
        import os
        import tempfile
        from conductor.storage import ConductorStorage
        from conductor.dispatch import (
            dispatch_task, reconcile_task, build_idempotency_key,
        )

        with tempfile.TemporaryDirectory() as d:
            s = ConductorStorage(os.path.join(d, "conductor.db"))
            s.initialize()

            obj = s.create_objective(title="full flow")
            run = s.create_run(obj["id"])
            task = s.create_task(obj["id"], run["id"], "Full flow", brief="hi")
            s.update_task_status(task["id"], "ready")

            external_task_id = "gw-external-1"

            with respx.mock(base_url="http://agents.test") as m:
                # Mock gateway responses: create task, run, then poll status.
                m.post("/tasks").respond(
                    201,
                    json={"task": {"id": external_task_id,
                                    "agent_id": "code-validator",
                                    "status": "created"}},
                )
                m.post(f"/tasks/{external_task_id}/run").respond(
                    202,
                    json={"task": {"id": external_task_id, "status": "running"}},
                )
                # Reconcile: gateway says "running" -> should be a no-op,
                # but later we'll switch it to completed + produce artifacts.
                m.get(f"/tasks/{external_task_id}").respond(
                    200,
                    json={"task": {"id": external_task_id,
                                    "status": "running"}},
                )
                m.get(f"/tasks/{external_task_id}/artifacts").respond(
                    200,
                    json={"artifacts": []},
                )

                client = HttpAgentsGatewayClient(cfg)
                result = dispatch_task(s, client, task["id"])
                assert result["status"] == "running"
                ar_id = result["id"]
                assert result["agents_gateway_task_id"] == external_task_id

                # Reconcile now -> running (no transition)
                first = reconcile_task(s, client, ar_id)
                assert first["status"] == "running"

            # Now simulate the gateway finishing while we're not connected:
            with respx.mock(base_url="http://agents.test") as m:
                m.get(f"/tasks/{external_task_id}").respond(
                    200,
                    json={"task": {"id": external_task_id,
                                    "status": "completed",
                                    "output": "ship OK"}},
                )
                m.get(f"/tasks/{external_task_id}/artifacts").respond(
                    200,
                    json={"artifacts": [
                        {"id": "art-1", "task_id": external_task_id,
                         "name": "report.log", "path": "/jobs/report.log",
                         "size_bytes": 4096},
                        {"id": "art-2", "task_id": external_task_id,
                         "name": "result.json", "path": "/jobs/result.json",
                         "size_bytes": 64},
                    ]},
                )
                client = HttpAgentsGatewayClient(cfg)
                # Reconcile again: should flip to completed and ingest artifacts.
                final = reconcile_task(s, client, ar_id)
                assert final["status"] == "completed"
                assert final["result_summary"].startswith("ship OK")
                assert len(final["artifact_refs"]) == 2
                names = {a["name"] for a in final["artifact_refs"]}
                assert names == {"report.log", "result.json"}

            # Verify the task is also completed (because reconcile_task updates it)
            t = s.get_task(task["id"])
            assert t["status"] == "completed"

    def test_full_flow_429_then_success(self, cfg):
        """429 then 200 retries through to a successful dispatch."""
        import os
        import tempfile
        from conductor.storage import ConductorStorage
        from conductor.dispatch import dispatch_task

        with tempfile.TemporaryDirectory() as d:
            s = ConductorStorage(os.path.join(d, "conductor.db"))
            s.initialize()

            obj = s.create_objective(title="retry flow")
            run = s.create_run(obj["id"])
            task = s.create_task(obj["id"], run["id"], "Retry me", brief="x")
            s.update_task_status(task["id"], "ready")

            external_id = "gw-retry-1"
            with respx.mock(base_url="http://agents.test") as m:
                # First create_task returns 429, second 201 — retry should happen.
                create_route = m.post("/tasks")
                # Note: respx side_effect lets us queue responses in order
                create_route.side_effect = [
                    httpx.Response(429, json={"detail": "slow down"}),
                    httpx.Response(201, json={"task": {"id": external_id, "status": "created"}}),
                ]
                m.post(f"/tasks/{external_id}/run").respond(
                    202,
                    json={"task": {"id": external_id, "status": "running"}},
                )

                # The default retry/jitter sleeps up to 1s on attempt 1;
                # make sleep fast.
                client = HttpAgentsGatewayClient(cfg, max_retries=2)
                client._backoff = lambda attempt: None  # no sleep in tests
                result = dispatch_task(s, client, task["id"])
                assert result["status"] == "running"
                assert result["agents_gateway_task_id"] == external_id
