"""Interaction handling — Composer answers agent interactions autonomously."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from conductor.composer.events import composer_emit
from conductor.composer.llm import ComposerLLMClient, LLMError
from conductor.composer.storage import ComposerStorage

logger = logging.getLogger(__name__)

__all__ = ["InteractionHandler"]


def _now_iso_safe() -> str:
    """ISO timestamp with timezone — safe even if the clock misbehaves."""
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return ""


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
        scheduler=None,
    ) -> None:
        self.storage = storage
        self.llm = llm_client
        self.agents_gateway = agents_gateway_client
        self.metrics = metrics
        self.conductor_storage = conductor_storage
        self.scheduler = scheduler

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
            result = self._restart_task(interaction, objective_id, plan, spec, task_context)
            return result

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

    def _restart_task(self, interaction, objective_id: str, plan: dict, spec: dict, task_context: str) -> dict | None:
        """Real restart: capture session, cancel old run, increment attempt,
        dispatch new GW task, persist new task ID, preserve attempt history,
        resolve/cancel the interaction.

        Failure-safe: if the replacement dispatch returns no task (the
        scheduler refused, or the gateway thunked out), we do NOT erase
        the old GW task ID, do NOT set status=running, persist the failed
        restart attempt + error, and mark the task ``blocked_external``
        so the supervisor leaves it alone for human attention.

        Order of operations for evidence safety: we capture the session
        BEFORE cancelling the old run, because ``cancel_task`` may
        terminate the tmux session and destroy the screen buffer we
        need to summarize the failure.
        """
        interaction_id = interaction.id
        task_id = interaction.task_id

        pt = self._find_plan_task_by_gw_id(task_id, plan)
        if not pt:
            logger.warning("Cannot restart: plan task not found for GW task %s", task_id)
            return None

        node_key = pt.get("node_key", "")
        plan_task_id = pt["id"]

        composer_emit(self.conductor_storage or self.storage, "composer.task_restarted", "",
                      objective_id=objective_id, task_id=task_id,
                      payload={"interaction_id": interaction_id, "node_key": node_key})

        # 1. Capture the old run's session and events FIRST. ``cancel_task``
        #    may terminate the tmux session and destroy the screen buffer,
        #    so we snapshot evidence before issuing the cancel.
        failure_context = ""
        session_id = ""
        try:
            session = self.agents_gateway.get_task_session(task_id)
            if session:
                session_id = session.id
                cap = self.agents_gateway.capture_session(session_id, lines=100)
                if cap and cap.capture:
                    failure_context = cap.capture
        except Exception as exc:
            logger.warning("Failed to capture session for restart: %s", exc)

        # 2. Cancel the old run — only AFTER evidence is captured.
        try:
            self.agents_gateway.cancel_task(task_id)
        except Exception:
            pass

        # 3. Increment attempt
        existing_meta = pt.get("metadata", {}) or {}
        attempt_history = existing_meta.get("attempt_history", [])
        if not isinstance(attempt_history, list):
            attempt_history = []
        attempt_history.append({
            "attempt": existing_meta.get("attempt", 1),
            "gw_task_id": task_id,
            "session_id": session_id,
            "failure_context": failure_context[:500] if failure_context else "",
        })
        new_attempt = int(existing_meta.get("attempt", 1)) + 1

        # 4. Dispatch a new Agents Gateway task through Scheduler
        dispatch_error = ""
        if self.scheduler is not None:
            # Build a TaskNode from the plan task for the scheduler
            from conductor.composer.models import TaskNode, VerificationSpec, VerificationCommand

            verification = pt.get("verification", {})
            commands = [
                VerificationCommand(**c) if isinstance(c, dict) else VerificationCommand()
                for c in verification.get("commands", [])
            ] if isinstance(verification, dict) else []
            live_e2e = verification.get("live_e2e") if isinstance(verification, dict) else None

            node = TaskNode(
                node_id=pt.get("node_key", ""),
                title=pt.get("title", ""),
                task_type=pt.get("task_type", "implementation"),
                goal=pt.get("goal", ""),
                dependencies=pt.get("dependencies", []),
                file_scope=pt.get("file_scope", []),
                ownership_notes=pt.get("ownership_notes", ""),
                harness_profile=pt.get("harness_profile", "opencode-deepseek"),
                required_skills=pt.get("required_skills", []),
                required_capabilities=pt.get("required_capabilities", []),
                verification=VerificationSpec(
                    required=verification.get("required", True) if isinstance(verification, dict) else True,
                    commands=commands,
                    live_e2e=live_e2e,
                ),
            )

            # Get repo info
            repo_url = spec.get("repository_url", "")
            base_branch = spec.get("base_branch", "master")

            # Use the scheduler's restart_failed_task which uses deepcopy to
            # preserve the original goal while appending failure context
            from conductor.composer.models import ComposerPlan, IntegrationNode
            # Reconstruct a minimal plan for the scheduler
            plan_obj = ComposerPlan(
                id=plan.get("id", ""),
                objective_id=objective_id,
                spec_id=plan.get("spec_id", ""),
                status="active",
                tasks=[node] if node.task_type != "integration" else [],
                integration=None,
            )

            try:
                dispatch_result = self.scheduler.restart_failed_task(
                    plan_obj, node, spec, objective_id,
                    repo_url, base_branch,
                    failure_context=failure_context,
                    attempt=new_attempt,
                )
            except Exception as exc:
                logger.error("Scheduler restart_failed_task raised: %s", exc)
                dispatch_result = None
                dispatch_error = f"scheduler_error: {exc}"

            new_gw_task_id = dispatch_result.get("gw_task_id") if dispatch_result else None
            partial_gw_task_id = dispatch_result.get("partial_gw_task_id") if dispatch_result else None
            run_failed = dispatch_result.get("run_failed") if dispatch_result else False
        else:
            new_gw_task_id = None
            partial_gw_task_id = None
            run_failed = False
            dispatch_error = "no_scheduler_configured"

        # ── Partial creation: create_harness_task succeeded but
        #     run_task failed.  The phantom task was cancelled by
        #     _dispatch_one; preserve both the old GW task ID and
        #     the partial ID, leave blocked_external.
        if run_failed:
            failure_meta = {
                **existing_meta,
                "attempt": new_attempt,
                "session_id": session_id,
                "failure_context": failure_context[:500] if failure_context else "",
                "attempt_history": attempt_history,
                "restarted_from_interaction": True,
                "last_restart_failed": True,
                "last_restart_error": "run_task failed after create_harness_task (partial creation)",
                "last_restart_at": _now_iso_safe(),
                "partial_gw_task_id": partial_gw_task_id,
            }
            self.storage.update_plan_task(
                plan_task_id,
                status="blocked_external",
                agents_gateway_task_id=task_id,  # preserve old GW task ID
                metadata=failure_meta,
            )
            decision = self.storage.create_interaction_decision(
                objective_id=objective_id,
                action="restart_task_failed",
                reply=(
                    f"Partial creation: task {partial_gw_task_id or '(none)'} "
                    f"was created but failed to start (run_task error). "
                    f"Original GW task {task_id} preserved."),
                decision_summary=(
                    f"Restart of {node_key}: create succeeded but run failed. "
                    f"Partial GW task {partial_gw_task_id} cancelled. "
                    f"Old GW task {task_id} preserved. Task blocked_external."),
                plan_task_id=plan_task_id,
                agents_gateway_interaction_id=interaction_id,
            )
            composer_emit(self.conductor_storage or self.storage,
                          "composer.task_restart_failed",
                          "partial creation: create_harness_task succeeded but run_task failed",
                          objective_id=objective_id, task_id=task_id,
                          payload={"interaction_id": interaction_id,
                                   "node_key": node_key,
                                   "partial_gw_task_id": partial_gw_task_id})
            return decision

        # ── Failure-safe restart ───────────────────────────────────────
        # If dispatch returned no task (or returned an empty string), do
        # NOT erase the previous GW task ID, do NOT claim the task is
        # running, and persist the failed restart attempt with the error.
        # Mark the task ``blocked_external`` so the supervisor stops
        # dispatching and waits for human/operator intervention.
        if not new_gw_task_id or new_gw_task_id == task_id:
            failure_meta = {
                **existing_meta,
                "attempt": new_attempt,
                "session_id": session_id,
                "failure_context": failure_context[:500] if failure_context else "",
                "attempt_history": attempt_history,
                "restarted_from_interaction": True,
                "last_restart_failed": True,
                "last_restart_error": dispatch_error or "no_gw_task_id",
                "last_restart_at": _now_iso_safe(),
            }
            self.storage.update_plan_task(
                plan_task_id,
                status="blocked_external",
                agents_gateway_task_id=task_id,  # preserve old GW task ID
                metadata=failure_meta,
            )

            decision = self.storage.create_interaction_decision(
                objective_id=objective_id,
                action="restart_task_failed",
                reply=f"Restart failed: {dispatch_error or 'no gw_task_id'}",
                decision_summary=(
                    f"Restart of {node_key} failed (no new gw_task_id); "
                    "task left blocked_external with old gw_task_id preserved"),
                plan_task_id=plan_task_id,
                agents_gateway_interaction_id=interaction_id,
            )

            composer_emit(self.conductor_storage or self.storage,
                          "composer.task_restart_failed", dispatch_error,
                          objective_id=objective_id, task_id=task_id,
                          payload={"interaction_id": interaction_id,
                                   "node_key": node_key,
                                   "error": dispatch_error})

            # Leave the interaction unresolved — operator may need to
            # re-investigate the original capture.
            return decision

        # 5. Persist the new task ID and attempt history
        merged = {
            **existing_meta,
            "attempt": new_attempt,
            "session_id": session_id,
            "failure_context": failure_context[:500] if failure_context else "",
            "attempt_history": attempt_history,
            "restarted_from_interaction": True,
            "last_restart_failed": False,
        }
        self.storage.update_plan_task(
            plan_task_id,
            status="running",
            agents_gateway_task_id=new_gw_task_id,
            metadata=merged,
        )

        # 6. Resolve/cancel the interaction
        try:
            self.agents_gateway.cancel_interaction(interaction_id)
        except Exception:
            pass

        # Persist decision
        decision = self.storage.create_interaction_decision(
            objective_id=objective_id,
            action="restart_task",
            reply="Task restarted with failure context.",
            decision_summary=f"Restarted task {node_key} (attempt {new_attempt})",
            plan_task_id=plan_task_id,
            agents_gateway_interaction_id=interaction_id,
        )

        composer_emit(self.conductor_storage or self.storage, "composer.interaction_answered", "",
                      objective_id=objective_id, task_id=task_id,
                      payload={"interaction_id": interaction_id, "action": "restart_task",
                               "new_gw_task_id": new_gw_task_id})

        if self.metrics:
            self.metrics.inc("conductor_composer_task_restarts_total")

        return decision

    def _mark_external_blocker(self, interaction, objective_id: str, reason: str) -> None:
        """Mark an interaction/task as an external blocker.

        Updates the matching plan task to ``blocked_external`` and persists
        the blocker reason in metadata so it survives process restart.
        """
        decision = self.storage.create_interaction_decision(
            objective_id=objective_id,
            action="mark_external_blocker",
            reply=reason,
            decision_summary="External blocker: missing credential/binary/service",
            agents_gateway_interaction_id=interaction.id,
        )

        # Update the matching plan task to blocked_external
        task_id = interaction.task_id
        try:
            plan_dict = self.storage.get_plan_by_objective(objective_id)
            if plan_dict:
                for pt in plan_dict.get("plan_tasks", []):
                    if pt.get("agents_gateway_task_id") == task_id:
                        existing_meta = pt.get("metadata", {}) or {}
                        merged = {
                            **existing_meta,
                            "blocker_reason": reason,
                            "blocked_by_interaction": interaction.id,
                        }
                        self.storage.update_plan_task(
                            pt["id"],
                            status="blocked_external",
                            metadata=merged,
                        )
                        break
        except Exception as exc:
            logger.warning("Failed to update plan task for external blocker: %s", exc)

        try:
            self.agents_gateway.cancel_interaction(interaction.id)
        except Exception:
            pass

        composer_emit(self.conductor_storage or self.storage, "composer.task_blocked_external", reason,
                      objective_id=objective_id, task_id=task_id,
                      payload={"interaction_id": interaction.id, "reason": reason})

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
