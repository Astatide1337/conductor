"""Composer LLM provider abstraction.

Provides:
- ``ComposerLLMClient`` — abstract interface.
- ``FakeComposerLLMClient`` — deterministic responses for tests / local E2E.
- ``HttpComposerLLMClient`` — OpenAI-compatible chat/completions client.

All responses are validated with Pydantic.  Unvalidated LLM output is
never executed.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from conductor.composer.models import (
    FinalSummaryResult,
    InteractionResult,
    NormalizedSpecResult,
    PlanResult,
)
from conductor.composer.prompts import (
    FINAL_SUMMARY_PROMPT,
    INTERACTION_PROMPT,
    NORMALIZE_PROMPT,
    PLAN_PROMPT,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ComposerLLMClient",
    "FakeComposerLLMClient",
    "HttpComposerLLMClient",
    "LLMError",
    "LLMRepairRequest",
]


class LLMError(Exception):
    """Raised when the Composer LLM provider fails after all retries."""


@dataclass
class LLMRepairRequest:
    """Return value when JSON validation fails and a repair is attempted."""

    raw_response: str
    errors: list[str]
    attempt: int


class ComposerLLMClient(ABC):
    """Abstract Composer LLM client."""

    @abstractmethod
    async def normalize_spec(self, raw_spec: str) -> NormalizedSpecResult: ...

    @abstractmethod
    async def create_plan(self, spec: str, context: str) -> PlanResult: ...

    @abstractmethod
    async def create_repair_plan(self, invalid_plan: str, errors: str, context: str) -> PlanResult: ...

    @abstractmethod
    async def answer_interaction(
        self,
        spec: str,
        task: str,
        interaction: str,
        capture: str,
    ) -> InteractionResult: ...

    @abstractmethod
    async def create_final_summary(
        self,
        title: str,
        status: str,
        tasks: str,
        interactions: str,
        verification: str,
    ) -> FinalSummaryResult: ...


# ── JSON extraction helper ──────────────────────────────────────────────


def _extract_json(text: str) -> str:
    """Try to find a JSON object or array inside ``text``."""
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    brace = text.find("{")
    if brace != -1:
        depth = 0
        for i in range(brace, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            if depth == 0:
                return text[brace : i + 1]
    return text


def _validate_or_raise(raw: str, model_cls):
    """Extract JSON, validate with Pydantic.  Raises LLMError on failure."""
    json_text = _extract_json(raw)
    try:
        return model_cls.model_validate_json(json_text)
    except Exception as exc:
        raise LLMError(f"LLM returned invalid {model_cls.__name__}: {exc}\nRaw: {raw[:500]}") from exc


# ── Fake LLM client ────────────────────────────────────────────────────────


@dataclass
class FakeComposerLLMClient(ComposerLLMClient):
    """Deterministic LLM client for tests and local E2E.

    Produces a plan with two parallel implementation tasks plus an
    integration task.  Interaction answers always reply with a spec-based
    answer.  Final summary is a concise structured result.
    """

    _calls: list[dict] = field(default_factory=list)

    @property
    def calls(self) -> list[dict]:
        return self._calls

    async def normalize_spec(self, raw_spec: str) -> NormalizedSpecResult:
        self._calls.append({"method": "normalize_spec", "raw_spec_len": len(raw_spec)})
        return NormalizedSpecResult(
            title="Composer build",
            goal=raw_spec.strip()[:500],
            repository={"url": "", "base_branch": "master"},
            requirements=["Implement the specification"],
            acceptance_criteria=["All tests pass"],
            required_live_verification=[],
            constraints=[],
            non_goals=[],
        )

    async def create_plan(self, spec: str, context: str) -> PlanResult:
        self._calls.append({"method": "create_plan", "spec_len": len(spec), "context_len": len(context)})
        return PlanResult(
            summary="Two parallel implementation tasks followed by one integration task.",
            tasks=[
                {
                    "node_id": "task_a",
                    "title": "Implement feature A",
                    "task_type": "implementation",
                    "goal": "Implement the first part of the specification.",
                    "dependencies": [],
                    "file_scope": ["src/"],
                    "harness_profile": "opencode-deepseek",
                    "required_skills": [],
                    "required_capabilities": [],
                    "verification": {"required": True, "commands": [{"name": "unit tests", "command": "uv run pytest -q", "required": True}]},
                },
                {
                    "node_id": "task_b",
                    "title": "Implement feature B",
                    "task_type": "implementation",
                    "goal": "Implement the second part of the specification.",
                    "dependencies": [],
                    "file_scope": ["tests/"],
                    "harness_profile": "opencode-deepseek",
                    "required_skills": [],
                    "required_capabilities": [],
                    "verification": {"required": True, "commands": [{"name": "unit tests", "command": "uv run pytest -q", "required": True}]},
                },
            ],
            integration={
                "required": True,
                "node_id": "integration",
                "title": "Integrate completed task branches",
                "dependencies": ["task_a", "task_b"],
                "verification": {"required": True, "commands": [{"name": "full test suite", "command": "uv run pytest -q", "required": True}]},
            },
        )

    async def create_repair_plan(self, invalid_plan: str, errors: str, context: str) -> PlanResult:
        self._calls.append({"method": "create_repair_plan", "pending_errors": errors[:100]})
        return await self.create_plan("", "")

    async def answer_interaction(
        self,
        spec: str,
        task: str,
        interaction: str,
        capture: str,
    ) -> InteractionResult:
        self._calls.append({"method": "answer_interaction"})
        return InteractionResult(
            action="reply",
            reply="Follow the specification and preserve backward compatibility. Record the assumption in the final report.",
            decision_summary="The specification already defines the expected behavior.",
        )

    async def create_final_summary(
        self,
        title: str,
        status: str,
        tasks: str,
        interactions: str,
        verification: str,
    ) -> FinalSummaryResult:
        self._calls.append({"method": "create_final_summary"})
        return FinalSummaryResult(
            summary=f"Objective '{title}' reached status: {status}.",
            assumptions=[],
            blockers=[],
        )


# ── HTTP LLM client (OpenAI-compatible) ────────────────────────────────────


class HttpComposerLLMClient(ComposerLLMClient):
    """OpenAI-compatible chat/completions client.

    Supports OpenRouter or any OpenAI-compatible endpoint.  Never logs
    API keys, prompts, or raw responses at INFO level.
    """

    MAX_REPAIR_RETRIES = 2

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 180.0,
        max_tokens: int = 8192,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _chat(self, user_msg: str) -> str:
        """Send a chat/completions request. Returns the assistant message text."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": user_msg}],
            "max_tokens": self._max_tokens,
            "temperature": 0.3,
        }
        url = f"{self._base_url}/chat/completions"
        client = await self._ensure_client()
        try:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM provider timed out: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"LLM provider returned {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM provider error: {exc}") from exc

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise LLMError("LLM provider returned no choices")
        return choices[0].get("message", {}).get("content", "")

    async def _chat_with_repair(self, user_msg: str, model_cls):
        """Call _chat, validate, optionally repair, retry."""
        raw = await self._chat(user_msg)
        try:
            return _validate_or_raise(raw, model_cls)
        except LLMError:
            pass

        for attempt in range(1, self.MAX_REPAIR_RETRIES + 1):
            logger.warning("LLM JSON validation failed, repair attempt %d/%d", attempt, self.MAX_REPAIR_RETRIES)
            repair_msg = (
                f"The previous response was not valid JSON matching the required schema. "
                f"Please respond with ONLY a valid JSON object matching the schema for {model_cls.__name__}.\n\n"
                f"Previous response:\n{raw[:2000]}"
            )
            raw = await self._chat(repair_msg)
            try:
                return _validate_or_raise(raw, model_cls)
            except LLMError:
                continue

        raise LLMError(f"LLM provider produced invalid output after {self.MAX_REPAIR_RETRIES} repair attempts")

    async def normalize_spec(self, raw_spec: str) -> NormalizedSpecResult:
        prompt = NORMALIZE_PROMPT.format(spec=raw_spec)
        return await self._chat_with_repair(prompt, NormalizedSpecResult)

    async def create_plan(self, spec: str, context: str) -> PlanResult:
        prompt = PLAN_PROMPT.format(spec=spec, context=context)
        return await self._chat_with_repair(prompt, PlanResult)

    async def create_repair_plan(self, invalid_plan: str, errors: str, context: str) -> PlanResult:
        repair_prompt = (
            f"A previously generated plan was invalid. Produce a corrected plan that passes validation.\n\n"
            f"Invalid plan:\n{invalid_plan[:3000]}\n\n"
            f"Validation errors:\n{errors}\n\n"
            f"Context:\n{context[:3000]}\n\n"
            f"Respond with ONLY a valid JSON object matching the PlanResult schema."
        )
        return await self._chat_with_repair(repair_prompt, PlanResult)

    async def answer_interaction(
        self,
        spec: str,
        task: str,
        interaction: str,
        capture: str,
    ) -> InteractionResult:
        prompt = INTERACTION_PROMPT.format(
            spec=spec[:2000],
            task=task[:2000],
            interaction=interaction[:2000],
            capture=capture[:2000],
        )
        return await self._chat_with_repair(prompt, InteractionResult)

    async def create_final_summary(
        self,
        title: str,
        status: str,
        tasks: str,
        interactions: str,
        verification: str,
    ) -> FinalSummaryResult:
        prompt = FINAL_SUMMARY_PROMPT.format(
            title=title,
            status=status,
            tasks=tasks[:3000],
            interactions=interactions[:2000],
            verification=verification[:2000],
        )
        return await self._chat_with_repair(prompt, FinalSummaryResult)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
