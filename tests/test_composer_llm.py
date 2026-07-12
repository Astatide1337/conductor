"""Tests for Composer LLM client abstraction — fake and HTTP."""

import pytest

from conductor.composer.llm import (
    ComposerLLMClient,
    FakeComposerLLMClient,
    HttpComposerLLMClient,
    LLMError,
    _extract_json,
)
from conductor.composer.models import (
    FinalSummaryResult,
    InteractionResult,
    NormalizedSpecResult,
    PlanResult,
)


class TestFakeComposerLLMClient:
    @pytest.fixture
    def client(self):
        return FakeComposerLLMClient()

    @pytest.mark.asyncio
    async def test_normalize_spec(self, client):
        result = await client.normalize_spec("Build a calculator")
        assert isinstance(result, NormalizedSpecResult)
        assert result.goal == "Build a calculator"
        assert len(result.requirements) > 0

    @pytest.mark.asyncio
    async def test_create_plan(self, client):
        result = await client.create_plan("spec text", "context text")
        assert isinstance(result, PlanResult)
        assert "Two parallel" in result.summary
        assert len(result.tasks) == 2
        assert result.tasks[0].node_id == "task_a"
        assert result.tasks[1].node_id == "task_b"
        assert result.integration.required is True
        assert set(result.integration.dependencies) == {"task_a", "task_b"}

    @pytest.mark.asyncio
    async def test_answer_interaction(self, client):
        result = await client.answer_interaction("spec", "task", "interaction", "capture")
        assert isinstance(result, InteractionResult)
        assert result.action == "reply"
        assert "specification" in result.reply.lower()

    @pytest.mark.asyncio
    async def test_create_final_summary(self, client):
        result = await client.create_final_summary("Title", "completed", "tasks", "interactions", "verification")
        assert isinstance(result, FinalSummaryResult)
        assert "completed" in result.summary

    @pytest.mark.asyncio
    async def test_calls_tracked(self, client):
        await client.normalize_spec("spec")
        await client.create_plan("spec", "ctx")
        assert len(client.calls) == 2
        assert client.calls[0]["method"] == "normalize_spec"
        assert client.calls[1]["method"] == "create_plan"


class TestExtractJson:
    def test_plain_json(self):
        text = '{"key": "value"}'
        assert _extract_json(text) == text

    def test_json_in_code_block(self):
        text = '```json\n{"key": "value"}\n```'
        assert "key" in _extract_json(text)

    def test_json_embedded_in_text(self):
        text = 'Here is the result: {"key": "value"} done.'
        extracted = _extract_json(text)
        assert "key" in extracted

    def test_plain_array(self):
        text = '[1, 2, 3]'
        assert _extract_json(text) == text

    def test_nested_braces(self):
        text = 'Here: {"outer": {"inner": "val"}} end'
        extracted = _extract_json(text)
        assert "inner" in extracted


class TestHttpComposerLLMClient:
    def test_init_strips_trailing_slash(self):
        c = HttpComposerLLMClient(
            base_url="https://api.example.com/v1/",
            api_key="sk-test",
            model="gpt-4",
        )
        assert c._base_url == "https://api.example.com/v1"

    def test_init_custom_timeout(self):
        c = HttpComposerLLMClient(
            base_url="https://api.example.com",
            api_key="sk-test",
            model="gpt-4",
            timeout=42.0,
        )
        assert c._timeout == 42.0

    def test_close(self):
        c = HttpComposerLLMClient(base_url="https://api.example.com", api_key="k", model="m")
        c.close()
