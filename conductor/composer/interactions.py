"""Interaction handling — Composer answers agent interactions autonomously."""

from __future__ import annotations

import logging

from conductor.composer.events import composer_emit
from conductor.composer.llm import ComposerLLMClient, LLMError
from conductor.composer.storage import ComposerStorage

logger = logging.getLogger(__name__)

__all__ = ["InteractionHandler"]


class InteractionHandler:
    """Discovers pending interactions, fetches session capture, asks the
    Composer LLM for a decision, validates it, replies through Agents
    Gateway, and persists the decision.
    """

    def __init__(
        self,
        storage: ComposerStorage,
        llm_client: ComposerLLMClient,
        agents_gateway_client,
        *,
        metrics=None,
        conductor_storage=None,
    ) -> None:
        self.storage = storage
        self.llm = llm_client
        self.agents_gateway = agents_gateway_client
        self.metrics = metrics
        self.conductor_storage = conductor_storage

    async def process_pending_interactions(
        self,
        objective_id: str,
        plan: dict,
        spec: dict,
    ) -> list[dict]:
        """Process only interactions belonging to this objective's GW tasks.

        Build the set of Agents Gateway task IDs in the plan, then filter
        listed interactions. Never answer another objective's interaction.
        """
        decisions: list[dict] = []

        gw_task_ids: set[str] = set()
        for pt in plan.get("plan_tasks", []):
            gw_id = pt.get("agents_gateway_task_id")
            if gw_id:
                gw_task_ids.add(gw_id)

        try:
            interactions = self.agents_gateway.list_interactions(status="pending")
        except Exception as exc:
            logger.warning("Failed to list interactions: %s", exc)
            return []

        my_interactions = [i for i in interactions if i.task_id in gw_task_ids]

        for interaction in my_interactions:
            decision = await self._handle_one(interaction, objective_id, plan, spec)
            if decision:
                decisions.append(decision)

        return decisions

    async def _handle_one(
        self,
        interaction,
        objective_id: str,
        plan: dict,
        spec: dict,
    ) -> dict | None:
        interaction_id = interaction.id
        task_id = interaction.task_id

        composer_emit(self.conductor_storage or self.storage, "composer.interaction_received", "",
                      objective_id=objective_id, task_id=task_id,
                      payload={"interaction_id": interaction_id})

        # Fetch session capture
        capture_text = ""
        if interaction.session_id:
            try:
                cap = self.agents_gateway.capture_session(interaction.session_id, lines=100)
                capture_text = cap.capture
            except Exception as exc:
                logger.warning("Failed to capture session %s: %s", interaction.session_id, exc)

        # Build context strings
        spec_summary = ""
        ns = spec.get("normalized_spec", {})
        if ns.get("goal"):
            spec_summary = ns["goal"][:500]

        task_context = ""
        for pt in plan.get("plan_tasks", []):
            if pt.get("agents_gateway_task_id") == task_id:
                task_context = pt.get("node_key", "")
                break

        interaction_text = interaction.prompt_excerpt or ""

        # Ask LLM
        try:
            result = await self.llm.answer_interaction(
                spec=spec_summary,
                task=task_context,
                interaction=interaction_text,
                capture=capture_text,
            )
        except LLMError as exc:
            logger.error("LLM failed to answer interaction: %s", exc)
            if self.metrics:
                self.metrics.inc("conductor_composer_llm_errors_total")
            # If it's a missing-credentials scenario, mark external blocker
            self._mark_external_blocker(interaction, objective_id, str(exc))
            return None

        action = result.action
        reply_text = result.reply
        summary = result.decision_summary

        if action == "mark_external_blocker":
            self._mark_external_blocker(interaction, objective_id, reply_text)
            return {"interaction_id": interaction_id, "action": "mark_external_blocker"}

        if action == "restart_task":
            # Find the plan task node and restart via scheduler integration
            pt = self._find_plan_task_by_gw_id(task_id, plan)
            if pt:
                composer_emit(self.conductor_storage or self.storage, "composer.task_restarted", "",
                              objective_id=objective_id, task_id=task_id,
                              payload={"interaction_id": interaction_id, "node_key": pt.get("node_key", "")})
                self.storage.update_plan_task(
                    pt["id"],
                    status="running",
                    metadata={**pt.get("metadata", {}), "restarted_from_interaction": True},
                )
                if self.metrics:
                    self.metrics.inc("conductor_composer_task_restarts_total")
            action = "reply"
            reply_text = "The task will be restarted with additional context."

        # Reply through Agents Gateway
        try:
            self.agents_gateway.reply_to_interaction(interaction_id, reply_text)
        except Exception as exc:
            logger.error("Failed to reply to interaction %s: %s", interaction_id, exc)
            return None

        # Persist decision
        plan_task_id = self._find_plan_task_id(task_id, plan)
        decision = self.storage.create_interaction_decision(
            objective_id=objective_id,
            action=action,
            reply=reply_text,
            decision_summary=summary,
            plan_task_id=plan_task_id,
            agents_gateway_interaction_id=interaction_id,
        )

        composer_emit(self.conductor_storage or self.storage, "composer.interaction_answered", "",
                      objective_id=objective_id, task_id=task_id,
                      payload={"interaction_id": interaction_id, "action": action})

        if self.metrics:
            self.metrics.inc("conductor_composer_interactions_answered_total")

        return decision

    def _mark_external_blocker(self, interaction, objective_id: str, reason: str) -> None:
        """Mark an interaction/task as an external blocker."""
        decision = self.storage.create_interaction_decision(
            objective_id=objective_id,
            action="mark_external_blocker",
            reply=reason,
            decision_summary="External blocker: missing credential/binary/service",
            agents_gateway_interaction_id=interaction.id,
        )
        try:
            self.agents_gateway.cancel_interaction(interaction.id)
        except Exception:
            pass

        composer_emit(self.conductor_storage or self.storage, "composer.task_blocked_external", reason,
                      objective_id=objective_id, task_id=interaction.task_id,
                      payload={"interaction_id": interaction.id})

    def _find_plan_task_id(self, gw_task_id: str, plan: dict) -> str | None:
        for pt in plan.get("plan_tasks", []):
            if pt.get("agents_gateway_task_id") == gw_task_id:
                return pt["id"]
        return None

    def _find_plan_task_by_gw_id(self, gw_task_id: str, plan: dict) -> dict | None:
        for pt in plan.get("plan_tasks", []):
            if pt.get("agents_gateway_task_id") == gw_task_id:
                return pt
        return None
