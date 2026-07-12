"""Tests for Composer interaction handling."""

import pytest

from conductor.clients.agents_gateway import MockAgentsGatewayClient
from conductor.composer.interactions import InteractionHandler
from conductor.composer.llm import FakeComposerLLMClient, LLMError, ComposerLLMClient
from conductor.composer.models import InteractionResult
from conductor.composer.storage import ComposerStorage
from conductor.storage import ConductorStorage


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def cstorage(db_path):
    s = ComposerStorage(db_path)
    s.initialize()
    return s


@pytest.fixture
def conductor_storage(db_path):
    s = ConductorStorage(db_path)
    s.initialize()
    return s


@pytest.fixture
def gw():
    client = MockAgentsGatewayClient()
    client.register_harness_profile("opencode-deepseek")
    return client


@pytest.fixture
def llm():
    return FakeComposerLLMClient()


@pytest.fixture
def handler(cstorage, llm, gw, conductor_storage):
    return InteractionHandler(cstorage, llm, gw, conductor_storage=conductor_storage)


@pytest.fixture
def objective_id(conductor_storage):
    return conductor_storage.create_objective(title="Test")["id"]


class TestProcessPendingInteractions:
    @pytest.mark.asyncio
    async def test_no_interactions(self, handler, objective_id):
        decisions = await handler.process_pending_interactions(objective_id, {"plan_tasks": []}, {})
        assert decisions == []

    @pytest.mark.asyncio
    async def test_pending_interaction_discovered_and_answered(self, handler, gw, objective_id, cstorage, conductor_storage):
        # Create a mock task with a pending interaction
        task = gw.create_harness_task({"title": "test", "composer_task_id": "task_a"})
        gw.run_task(task.id)
        gw.set_task_waiting(task.id)
        interaction = gw.create_mock_interaction(task_id=task.id, prompt="What should I do?")

        # Create spec and plan in storage
        cstorage.create_spec(objective_id, "Test", "raw")

        decisions = await handler.process_pending_interactions(
            objective_id,
            {"plan_tasks": [{"agents_gateway_task_id": task.id, "id": "ptask_1", "node_key": "task_a"}]},
            {"normalized_spec": {"goal": "build"}},
        )
        assert len(decisions) == 1
        assert decisions[0]["action"] == "reply"
        assert "specification" in decisions[0]["reply"].lower()

        # Interaction status should be answered
        interactions = gw.list_interactions(status="pending")
        assert len(interactions) == 0  # answered, so not pending anymore

    @pytest.mark.asyncio
    async def test_interaction_persisted(self, handler, gw, objective_id, cstorage):
        task = gw.create_harness_task({"title": "test"})
        gw.run_task(task.id)
        gw.set_task_waiting(task.id)
        interaction = gw.create_mock_interaction(task_id=task.id, prompt="Need help?")

        cstorage.create_spec(objective_id, "Test", "raw")

        decisions = await handler.process_pending_interactions(
            objective_id,
            {"plan_tasks": [{"agents_gateway_task_id": task.id, "id": "ptask_1", "node_key": "task_a"}]},
            {"normalized_spec": {"goal": "build"}},
        )
        assert len(decisions) == 1

        # Check decision persisted in storage
        stored_decisions = cstorage.list_interaction_decisions(objective_id)
        assert len(stored_decisions) == 1
        assert stored_decisions[0]["action"] == "reply"

    @pytest.mark.asyncio
    async def test_session_capture_fetched(self, handler, gw, objective_id, cstorage):
        # Create task and session
        task = gw.create_harness_task({"title": "test"})
        gw.run_task(task.id)
        session = gw.create_mock_session(task.id)
        gw.set_task_waiting(task.id)
        interaction = gw.create_mock_interaction(task_id=task.id, prompt="Need guidance")

        cstorage.create_spec(objective_id, "Test", "raw")

        decisions = await handler.process_pending_interactions(
            objective_id,
            {"plan_tasks": [{"agents_gateway_task_id": task.id, "id": "ptask_1", "node_key": "task_a"}]},
            {"normalized_spec": {"goal": "build"}},
        )
        assert len(decisions) == 1

    @pytest.mark.asyncio
    async def test_normal_ambiguity_does_not_escalate(self, handler, gw, objective_id, cstorage):
        """Ambiguity should be answered, not escalated to human."""
        task = gw.create_harness_task({"title": "test"})
        gw.run_task(task.id)
        gw.set_task_waiting(task.id)
        interaction = gw.create_mock_interaction(
            task_id=task.id,
            prompt="Should I use approach A or approach B?",
        )

        cstorage.create_spec(objective_id, "Test", "raw")

        decisions = await handler.process_pending_interactions(
            objective_id,
            {"plan_tasks": [{"agents_gateway_task_id": task.id, "id": "ptask_1", "node_key": "task_a"}]},
            {"normalized_spec": {"goal": "build"}},
        )
        assert len(decisions) == 1
        assert decisions[0]["action"] == "reply"  # not mark_external_blocker


class TestMissingCredentialsBlocker:
    @pytest.mark.asyncio
    async def test_missing_credentials_becomes_external_blocker(self, cstorage, gw, conductor_storage, objective_id):
        """When LLM action is mark_external_blocker, mark as external blocker."""
        from dataclasses import dataclass, field
        from typing import List

        # Use a custom LLM that returns mark_external_blocker
        class BlockingLLM(FakeComposerLLMClient):
            async def answer_interaction(self, spec, task, interaction, capture):
                return InteractionResult(
                    action="mark_external_blocker",
                    reply="Missing API credentials",
                    decision_summary="Credential blocked",
                )

        blocking_llm = BlockingLLM()
        handler = InteractionHandler(cstorage, blocking_llm, gw, conductor_storage=conductor_storage)

        cstorage.create_spec(objective_id, "Test", "raw")

        task = gw.create_harness_task({"title": "test"})
        gw.run_task(task.id)
        gw.set_task_waiting(task.id)
        interaction = gw.create_mock_interaction(task_id=task.id, prompt="No API key!")

        decisions = await handler.process_pending_interactions(
            objective_id,
            {"plan_tasks": [{"agents_gateway_task_id": task.id, "id": "ptask_1", "node_key": "task_a"}]},
            {"normalized_spec": {"goal": "build"}},
        )
        assert len(decisions) == 1
        assert decisions[0]["action"] == "mark_external_blocker"

        # Interaction should be cancelled
        assert gw.get_interaction(interaction.id).status == "cancelled"


class TestLLMErrorHandling:
    @pytest.mark.asyncio
    async def test_llm_error_does_not_crash(self, cstorage, gw, conductor_storage, objective_id):
        class ErrorLLM(ComposerLLMClient):
            async def normalize_spec(self, raw_spec):
                raise LLMError("provider down")

            async def create_plan(self, spec, context):
                raise LLMError("provider down")

            async def answer_interaction(self, spec, task, interaction, capture):
                raise LLMError("provider down")

            async def create_final_summary(self, title, status, tasks, interactions, verification):
                raise LLMError("provider down")

        handler = InteractionHandler(cstorage, ErrorLLM(), gw, conductor_storage=conductor_storage)
        cstorage.create_spec(objective_id, "Test", "raw")

        task = gw.create_harness_task({"title": "test"})
        gw.run_task(task.id)
        gw.set_task_waiting(task.id)
        gw.create_mock_interaction(task_id=task.id, prompt="Help?")

        decisions = await handler.process_pending_interactions(
            objective_id,
            {"plan_tasks": [{"agents_gateway_task_id": task.id, "id": "ptask_1", "node_key": "task_a"}]},
            {"normalized_spec": {"goal": "build"}},
        )
        # Error blocks — should return empty list and persist an external blocker decision
        assert decisions == []
        stored = cstorage.list_interaction_decisions(objective_id)
        assert len(stored) == 1
        assert stored[0]["action"] == "mark_external_blocker"
