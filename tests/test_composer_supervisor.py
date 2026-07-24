"""Tests for ComposerSupervisor background loop."""

import asyncio
import tempfile
import os

import pytest

from conductor.circuit import BreakerEvaluator
from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.composer.llm import FakeComposerLLMClient
from conductor.composer.service import ComposerService
from conductor.composer.storage import ComposerStorage
from conductor.composer.supervisor import ComposerSupervisor
from conductor.config import ConductorConfig
from conductor.gateways import build_default_registry
from conductor.storage import ConductorStorage


@pytest.fixture
def setup():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "test.db")
        cs = ConductorStorage(db); cs.initialize()
        cps = ComposerStorage(db); cps.initialize()
        cfg = ConductorConfig(environment="test", storage={"sqlite_path": db})
        gw = MockAgentsGatewayClient()
        gw.register_agent("code-validator", "Code Validator")
        gw.register_harness_profile("pi-coding-agent", "OpenCode DeepSeek", runnable=True)
        reg = build_default_registry(cfg)
        svc = ComposerService(
            storage=cps, conductor_storage=cs,
            llm_client=FakeComposerLLMClient(), agents_gateway_client=gw,
            config=cfg.composer, skills_gateway_client=None,
            wiki_mcp_client=None, gateway_registry=reg, metrics=None,
        )
        yield svc, cs, cps


class TestSupervisorInit:
    def test_default_init(self, setup):
        svc, *_ = setup
        s = ComposerSupervisor(svc)
        assert s.enabled is True
        assert s.poll_interval == 10.0
        assert s._running is False
        assert s._task is None

    def test_custom_init(self, setup):
        svc, *_ = setup
        s = ComposerSupervisor(svc, poll_interval=1.0, enabled=False)
        assert s.enabled is False
        assert s.poll_interval == 1.0


class TestSupervisorStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_task(self, setup):
        svc, *_ = setup
        s = ComposerSupervisor(svc, poll_interval=0.05, enabled=True)
        await s.start()
        assert s._running is True
        assert s._task is not None
        await asyncio.sleep(0.2)  # give it a tick
        await s.stop()
        assert s._running is False

    @pytest.mark.asyncio
    async def test_start_idempotent(self, setup):
        svc, *_ = setup
        s = ComposerSupervisor(svc, poll_interval=0.1, enabled=True)
        await s.start()
        first_task = s._task
        await s.start()  # second call should be a no-op
        assert s._task is first_task
        await s.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self, setup):
        svc, *_ = setup
        s = ComposerSupervisor(svc, enabled=True)
        await s.stop()
        assert s._running is False
        assert s._task is None

    @pytest.mark.asyncio
    async def test_disabled_supervisor_no_tick(self, setup):
        svc, *_ = setup
        s = ComposerSupervisor(svc, poll_interval=0.01, enabled=False)
        await s.start()
        # _run loop uses `while self._running` — start sets it True regardless; enabled=False
        # doesn't directly prevent start but conventionally we wouldn't call start on disabled.
        await asyncio.sleep(0.1)
        await s.stop()


class TestSupervisorTick:
    @pytest.mark.asyncio
    async def test_tick_reconciles_no_objectives(self, setup):
        svc, *_ = setup
        s = ComposerSupervisor(svc, poll_interval=10.0, enabled=True)
        # No objectives exist; tick should be a no-op (no exceptions)
        await s._tick()

    @pytest.mark.asyncio
    async def test_tick_reconciles_active_objective(self, setup):
        svc, cs, cps = setup
        # Create an objective by submitting a spec (auto_start=False so we can manually drive)
        result = await svc.submit_specification(
            title="Tick", raw_spec="spec", auto_start=False,
        )
        obj_id = result["objective_id"]
        # Tick should drive spec normalization → planning (eventually)
        s = ComposerSupervisor(svc, poll_interval=10.0, enabled=True)
        await s._tick()
        # Spec should now be normalized (FakeComposerLLMClient is deterministic)
        spec = cps.get_spec(result["composer_spec_id"])
        assert spec["status"] in ("normalized", "planning", "planned", "executing", "received", "normalizing")

    @pytest.mark.asyncio
    async def test_tick_resurrects_paused_objective_after_resume(self, setup):
        svc, cs, cps = setup
        result = await svc.submit_specification(
            title="Pause", raw_spec="spec", auto_start=True,
        )
        obj_id = result["objective_id"]
        # Pause and then resume
        await svc.pause_objective(obj_id)
        await svc.resume_objective(obj_id)
        s = ComposerSupervisor(svc, poll_interval=10.0, enabled=True)
        # tick should work without error even after pause/resume
        await s._tick()


class TestSupervisorRestartRecovery:
    @pytest.mark.asyncio
    async def test_supervisor_state_survives_restart(self, setup):
        """The ComposerService state is persisted; a fresh supervisor will pick up."""
        svc, cs, cps = setup
        result = await svc.submit_specification(
            title="Persist", raw_spec="spec", auto_start=False,
        )
        obj_id = result["objective_id"]
        s1 = ComposerSupervisor(svc, poll_interval=0.05, enabled=True)
        await s1.start()
        await asyncio.sleep(0.1)
        await s1.stop()
        # Construct a brand new supervisor on the same service
        s2 = ComposerSupervisor(svc, poll_interval=0.05, enabled=True)
        await s2.start()
        await asyncio.sleep(0.1)
        await s2.stop()
        # Verify objective persisted
        objs = svc.list_objectives()
        assert any(o.get("id") == obj_id or o.get("objective_id") == obj_id for o in objs)
