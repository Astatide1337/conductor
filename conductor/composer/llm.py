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


class LLMBillingError(LLMError):
    """LLM provider returned a payment-required (402) or rate-limited (429)
    response. Distinct from generic LLMError so upper layers can apply a
    graceful fallback (e.g. drop to a free-tier model)."""


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
                    "harness_profile": "pi-coding-agent",
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
                    "harness_profile": "pi-coding-agent",
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

    Falls back from a configured paid model (``primary_model``) to a
    configured free-tier model (``fallback_model``) on payment-required
    (402) or rate-limited (429) responses from the upstream provider.
    This is the primary defense against running out of mid-run credit
    — the planner can degrade to a free model without operator input.
    """

    MAX_REPAIR_RETRIES = 2

    BILLING_STATUSES = (402, 429)

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 180.0,
        max_tokens: int = 8192,
        fallback_model: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._primary_model = model
        self._fallback_model = fallback_model
        self._current_model = model
        self._fell_back = False
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._client: httpx.AsyncClient | None = None

    @property
    def active_model(self) -> str:
        """The model the next request will use. Useful for logs / API."""
        return self._current_model

    @property
    def did_fall_back(self) -> bool:
        """True iff a 402/429 already forced us to the fallback model."""
        return self._fell_back

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _chat(self, user_msg: str) -> str:
        """Send a chat/completions request. Returns the assistant message text.

        On a billing-style status (402 payment-required, 429 rate-limited)
        and a configured ``fallback_model``, the client retries once with
        the fallback model and flips its ``active_model`` so later calls
        also avoid the paid endpoint. Other HTTP errors propagate as
        LLMError as before.
        """
        body = {
            "model": self._current_model,
            "messages": [{"role": "user", "content": user_msg}],
            "max_tokens": self._max_tokens,
            "temperature": 0.3,
        }
        url = f"{self._base_url}/chat/completions"
        client = await self._ensure_client()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM provider timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM provider error: {exc}") from exc

        if resp.status_code in self.BILLING_STATUSES:
            if not self._fell_back and self._fallback_model \
                    and self._fallback_model != self._primary_model:
                # Switch to the fallback model and retry exactly once.
                self._current_model = self._fallback_model
                self._fell_back = True
                body["model"] = self._current_model
                resp = await client.post(url, headers=headers, json=body)
                if resp.status_code >= 400:
                    raise LLMBillingError(
                        f"LLM provider {resp.status_code} on fallback "
                        f"model {self._fallback_model}: "
                        f"{resp.text[:300]}"
                    )
            else:
                raise LLMBillingError(
                    f"LLM provider {resp.status_code} "
                    f"(model={self._current_model}): {resp.text[:300]}"
                )
        if resp.status_code >= 400:
            raise LLMError(
                f"LLM provider returned {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise LLMError("LLM provider returned no choices")
        return choices[0].get("message", {}).get("content", "") or ""


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
