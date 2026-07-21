"""Tests for Composer LLM client abstraction — fake and HTTP."""

import pytest

from conductor.composer.llm import (
    ComposerLLMClient,
    FakeComposerLLMClient,
    HttpComposerLLMClient,
    LLMError,
    LLMBillingError,
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

    @pytest.mark.asyncio
    async def test_close(self):
        c = HttpComposerLLMClient(base_url="https://api.example.com", api_key="k", model="m")
        await c.close()

    @pytest.mark.asyncio
    async def test_402_falls_back_to_free(self):
        """A 402 on the primary model triggers a single retry against
        the configured fallback model; the resulting active model flips
        to the fallback so all subsequent requests stay on the free
        tier."""
        import respx
        import httpx

        primary_payload_used = []
        fallback_payload_used = []

        def primary_router(req):
            primary_payload_used.append(req)
            return httpx.Response(402, json={"error": {"message": "no credits"}})

        def fallback_router(req):
            fallback_payload_used.append(req)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "{\"ok\":true}"}}
                    ]
                },
            )

        with respx.mock:
            respx.post("https://api.example.com/chat/completions").mock(
                side_effect=[
                    httpx.Response(402, json={"error": {"message": "no credits"}}),
                    httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": '{"ok":true}',
                                    }
                                }
                            ]
                        },
                    ),
                ]
            )
            c = HttpComposerLLMClient(
                base_url="https://api.example.com/",
                api_key="sk-test",
                model="deepseek-v4-flash",
                fallback_model="openai/gpt-oss-20b:free",
                max_tokens=120,
            )
            result = await c._chat("ping")
            assert result == '{"ok":true}'
            assert c.did_fall_back is True
            assert c.active_model == "openai/gpt-oss-20b:free"

            # Second call should NOT re-hit the primary; already on fallback.
            primary_calls = [r for r in respx.calls if r.request.url.path == "/chat/completions"]
            assert len(primary_calls) == 2
            first_body = primary_calls[0].request.content.decode()
            second_body = primary_calls[1].request.content.decode()
            assert '"model":"deepseek-v4-flash"' in first_body
            assert '"model":"openai/gpt-oss-20b:free"' in second_body

    @pytest.mark.asyncio
    async def test_402_no_fallback_raises_billing(self):
        """When no fallback is configured, a 402 surfaces as LLMBillingError."""
        import respx
        import httpx

        with respx.mock:
            respx.post("https://api.example.com/chat/completions").mock(
                return_value=httpx.Response(
                    402,
                    json={"error": {"message": "no credits"}},
                )
            )
            c = HttpComposerLLMClient(
                base_url="https://api.example.com",
                api_key="sk-test",
                model="deepseek-v4-flash",
            )
            with pytest.raises(LLMBillingError):
                await c._chat("ping")
            assert c.did_fall_back is False

    @pytest.mark.asyncio
    async def test_429_also_triggers_fallback(self):
        """429 rate-limited responses fall back the same way as 402."""
        import respx
        import httpx

        with respx.mock:
            respx.post("https://api.example.com/chat/completions").mock(
                side_effect=[
                    httpx.Response(429, json={"error": {"message": "rate limit"}}),
                    httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": '{"ok":true}',
                                    }
                                }
                            ]
                        },
                    ),
                ]
            )
            c = HttpComposerLLMClient(
                base_url="https://api.example.com",
                api_key="sk-test",
                model="deepseek-v4-flash",
                fallback_model="openai/gpt-oss-20b:free",
            )
            await c._chat("ping")
            assert c.did_fall_back is True
            assert c.active_model == "openai/gpt-oss-20b:free"
