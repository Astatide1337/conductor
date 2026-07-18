"""Composer service — high-level API for the Composer engine.

Coordinates normalization, context building, planning, scheduling,
supervision, interactions, integration, verification, and reports.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Optional

from conductor.composer.context import build_composer_context, context_to_prompt
from conductor.composer.events import composer_emit
from conductor.composer.interactions import InteractionHandler
from conductor.composer.integration import IntegrationDispatcher
from conductor.composer.llm import ComposerLLMClient, FakeComposerLLMClient, HttpComposerLLMClient, LLMError
from conductor.composer.models import (
    ComposerPlan,
    ComposerSpec,
    IntegrationNode,
    NormalizedSpec,
    NormalizedSpecResult,
    PlanResult,
    SpecRepository,
    TaskNode,
    VerificationCommand,
    VerificationSpec,
)
from conductor.composer.planner import validate_plan_result
from conductor.composer.reports import ReportGenerator
from conductor.composer.scheduler import Scheduler
from conductor.composer.storage import ComposerStorage
from conductor.composer.verification import ObjectiveCompletion, VerificationContract
from conductor.config import ComposerConfig
from conductor.storage import ConductorStorage

logger = logging.getLogger(__name__)

__all__ = ["ComposerService"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _uid() -> str:
    return str(uuid.uuid4())


class ComposerService:
    """High-level Composer API.

    Holds references to Composer storage, LLM client, Agents Gateway client,
    Skills Gateway client, and the gateway registry.  Used by the HTTP
    routes and MCP tools.
    """

    def __init__(
        self,
        storage: ComposerStorage,
        conductor_storage: ConductorStorage,
        llm_client: ComposerLLMClient,
        agents_gateway_client,
        *,
        config: ComposerConfig | None = None,
        skills_gateway_client=None,
        wiki_mcp_client=None,
        mcp_gateway_client=None,
        gateway_registry=None,
        metrics=None,
    ) -> None:
        self.storage = storage
        self.conductor_storage = conductor_storage
        self.llm = llm_client
        self.agents_gateway = agents_gateway_client
        self.skills_gateway = skills_gateway_client
        self.wiki_mcp = wiki_mcp_client
        self.mcp_gateway = mcp_gateway_client
        self.gateway_registry = gateway_registry
        self.metrics = metrics
        self.config = config or ComposerConfig()

        self.scheduler = Scheduler(
            storage,
            agents_gateway_client,
            max_parallel_tasks=self.config.max_parallel_tasks,
            metrics=metrics,
            conductor_storage=conductor_storage,
        )
        self.interaction_handler = InteractionHandler(
            storage, llm_client, agents_gateway_client, metrics=metrics,
            conductor_storage=conductor_storage,
            scheduler=self.scheduler,
        )
        self.integration_dispatcher = IntegrationDispatcher(
            storage,
            agents_gateway_client,
            integration_harness_profile=self.config.integration_harness_profile,
            metrics=metrics,
            conductor_storage=conductor_storage,
        )
        self.report_generator = ReportGenerator(storage, report_dir=self.config.report_dir)
        self.verification_contract = VerificationContract(storage)

    # ── Spec submission ─────────────────────────────────────────────────

    async def submit_specification(
        self,
        title: str,
        raw_spec: str,
        repository: dict | None = None,
        auto_start: bool = True,
    ) -> dict:
        """Submit a finalized specification.  Creates Conductor objective + Composer spec
        and returns immediately — the supervisor drives normalization/planning/dispatch.
        """
        repo_url = (repository or {}).get("url", "")
        base_branch = (repository or {}).get("base_branch", "master")

        # Create Conductor objective
        obj_id = f"obj_{_uid()}"
        now = _now_iso()
        obj = self.conductor_storage.create_objective(
            title=title,
            description=raw_spec[:500],
            created_by="composer",
            metadata={"composer": True, "repository": repository or {}, "composer_auto_start": auto_start},
        )
        obj_id = obj["id"]

        # Create a run
        run = self.conductor_storage.create_run(
            obj_id, planner_mode="composer", metadata={"composer": True},
        )

        # Create Composer spec with persisted repository
        spec = self.storage.create_spec(obj_id, title, raw_spec, repository_url=repo_url, base_branch=base_branch)
        spec_id = spec["id"]

        composer_emit(self.conductor_storage, "composer.objective_received", title,
                      objective_id=obj_id, payload={"spec_id": spec_id})

        # Mark as ready so the supervisor picks it up — do NOT await normalization/planning
        status = "received" if auto_start else "received"
        self.storage.update_spec(spec_id, status=status)

        return {
            "objective_id": obj_id,
            "composer_spec_id": spec_id,
            "status": status,
            "auto_start": auto_start,
        }

    # ── Objective lifecycle ─────────────────────────────────────────────

    async def start_objective(self, objective_id: str) -> dict:
        """Start the Composer pipeline for an objective.

        Idempotent per transitional state — safe to call repeatedly from
        the supervisor after a process restart.  Each state is advanced
        exactly one step per call:

        - received    → normalize
        - normalizing → normalize (safe rerun)
        - normalized   → create and activate plan
        - planning     → create and activate plan (safe rerun / restore draft)
        - planned      → dispatch ready tasks
        """
        spec = self.storage.get_spec_by_objective(objective_id)
        if not spec:
            return {"error": "spec not found", "objective_id": objective_id}

        status = spec.get("status", "received")

        # Paused objectives must not be advanced by the supervisor
        if status == "paused":
            return {"objective_id": objective_id, "status": "paused"}

        if status in ("received", "normalizing"):
            await self._normalize_spec(spec)

        spec = self.storage.get_spec_by_objective(objective_id)
        status = spec.get("status", "received")

        if status in ("normalized", "planning"):
            await self._create_and_activate_plan(spec, objective_id)

        spec = self.storage.get_spec_by_objective(objective_id)
        status = spec.get("status", "received")

        if status in ("planned",):
            dispatched = await self._dispatch_ready(objective_id)
            if dispatched:
                self.storage.update_spec(spec["id"], status="executing")

        return {"objective_id": objective_id, "status": self._get_objective_status(objective_id)}

    async def _normalize_spec(self, spec: dict) -> dict:
        spec_id = spec["id"]
        self.storage.update_spec(spec_id, status="normalizing")
        composer_emit(self.conductor_storage, "composer.spec_normalizing", "",
                      objective_id=spec["objective_id"], payload={"spec_id": spec_id})

        try:
            result = await self.llm.normalize_spec(spec["raw_spec"])
        except LLMError as exc:
            logger.error("LLM normalization failed: %s", exc)
            if self.metrics:
                self.metrics.inc("conductor_composer_llm_errors_total")
            self.storage.update_spec(spec_id, status="blocked_external")
            return spec

        normalized_title = result.title or spec["title"]

        # Preserve user-supplied repository over LLM-generated values
        user_url = spec.get("repository_url", "")
        user_branch = spec.get("base_branch", "master")
        llm_repo = result.repository if isinstance(result.repository, dict) else {}
        repo_url = user_url or llm_repo.get("url", "")
        repo_branch = user_branch if user_branch != "master" else llm_repo.get("base_branch", user_branch)

        normalized = NormalizedSpec(
            goal=result.goal,
            repository=SpecRepository(url=repo_url, base_branch=repo_branch),
            requirements=result.requirements,
            acceptance_criteria=result.acceptance_criteria,
            required_live_verification=result.required_live_verification,
            constraints=result.constraints,
            non_goals=result.non_goals,
        )

        updated = self.storage.update_spec(
            spec_id,
            normalized_spec=normalized.model_dump(),
            status="normalized",
            title=normalized_title,
        )
        composer_emit(self.conductor_storage, "composer.spec_normalized", "",
                      objective_id=spec["objective_id"], payload={"spec_id": spec_id})
        return updated or spec

    async def _build_context(self, spec: dict, objective_id: str) -> ComposerContext | None:
        repo_info = spec.get("normalized_spec", {}).get("repository", {})
        repo_url = repo_info.get("url", "") if isinstance(repo_info, dict) else ""

        ctx = build_composer_context(
            objective_id,
            spec,
            registry=self.gateway_registry,
            agents_gateway_client=self.agents_gateway,
            skills_gateway_client=self.skills_gateway,
            wiki_mcp_client=self.wiki_mcp,
            mcp_gateway_client=self.mcp_gateway,
        )

        # If required repo could not be accessed, block the objective
        if repo_url and ctx.project_context.get("repo_required") and ctx.project_context.get("access_error"):
            error = f"Required repository inaccessible: {ctx.project_context['access_error']}"
            self.storage.update_spec(spec["id"], status="blocked_external")
            composer_emit(self.conductor_storage, "composer.objective_blocked_external", error,
                          objective_id=objective_id, payload={"reason": "repository_access_failed"})
            return None

        composer_emit(self.conductor_storage, "composer.context_built", "",
                      objective_id=objective_id,
                      payload={"harness_count": len(ctx.harness_profiles),
                               "skill_count": len(ctx.skills)})
        return ctx

    async def _create_and_activate_plan(self, spec: dict, objective_id: str) -> dict | None:
        # ── Idempotency guard ────────────────────────────────────────
        # If a crash leaves spec.status=planning after we already inserted
        # a plan row, do NOT generate another plan. Reuse (and activate)
        # the existing draft/active plan so subsequent start_objective
        # tick converges instead of duplicating plan rows.
        existing = self.storage.get_plan_by_objective(objective_id)
        if existing and existing.get("status") in ("draft", "active", "completed"):
            if existing["status"] == "draft":
                self.storage.update_plan(existing["id"], status="active",
                                        activated_at=_now_iso())
                composer_emit(self.conductor_storage,
                              "composer.plan_activated", "",
                              objective_id=objective_id,
                              payload={"plan_id": existing["id"],
                                       "reused": True})
            self.storage.update_spec(spec["id"], status="planned")
            if self.metrics:
                self.metrics.inc("conductor_composer_plans_reused_total")
            return {"plan_id": existing["id"], "reused": True}

        self.storage.update_spec(spec["id"], status="planning")
        composer_emit(self.conductor_storage, "composer.planning_started", "",
                      objective_id=objective_id)

        # Build context for planning
        ctx = await self._build_context(spec, objective_id)
        if ctx is None:
            return None

        context_str = context_to_prompt(ctx)
        spec_str = str(spec.get("normalized_spec", {}))

        plan_result = None
        validation = None
        max_repairs = getattr(self.config, "max_repair_retries", 3)

        for attempt in range(max_repairs + 1):
            try:
                if attempt == 0:
                    plan_result = await self.llm.create_plan(spec=spec_str, context=context_str)
                else:
                    plan_result = await self.llm.create_repair_plan(
                        invalid_plan=str(plan_result.model_dump()),
                        errors="; ".join(validation.errors) if validation else "",
                        context=context_str,
                    )
            except LLMError as exc:
                logger.error("LLM planning failed: %s", exc)
                if self.metrics:
                    self.metrics.inc("conductor_composer_llm_errors_total")
                self.storage.update_spec(spec["id"], status="blocked_external")
                return None

            composer_emit(self.conductor_storage, "composer.plan_generated", "",
                          objective_id=objective_id,
                          payload={"task_count": len(plan_result.tasks),
                                   "attempt": attempt + 1})

            # Validate plan
            validation = validate_plan_result(plan_result, ctx)
            if validation.valid:
                if validation.warnings:
                    logger.info("Plan validation warnings: %s", validation.warnings)
                break

            composer_emit(self.conductor_storage, "composer.plan_validation_failed",
                          "; ".join(validation.errors),
                          objective_id=objective_id,
                          payload={"errors": validation.errors, "attempt": attempt + 1})
        else:
            # All repair attempts exhausted
            self.storage.update_spec(spec["id"], status="blocked_external")
            return None

        if validation.warnings:
            logger.info("Plan validation warnings: %s", validation.warnings)

        # Create ComposerPlan
        plan_id = f"plan_{_uid()}"
        tasks = [
            TaskNode(
                node_id=t.node_id,
                title=t.title,
                task_type=t.task_type,
                goal=t.goal,
                dependencies=t.dependencies,
                file_scope=t.file_scope,
                ownership_notes=t.ownership_notes,
                harness_profile=t.harness_profile,
                required_skills=t.required_skills,
                required_capabilities=t.required_capabilities,
                verification=t.verification,
            )
            for t in plan_result.tasks
        ]
        integration = IntegrationNode(
            required=plan_result.integration.required,
            node_id=plan_result.integration.node_id,
            title=plan_result.integration.title,
            task_type="integration",
            goal="Integration: combine task branches and run full verification",
            dependencies=plan_result.integration.dependencies,
            verification=plan_result.integration.verification,
        )

        plan = ComposerPlan(
            id=plan_id,
            objective_id=objective_id,
            spec_id=spec["id"],
            version=1,
            status="draft",
            tasks=tasks,
            integration=integration,
            created_at=_now_iso(),
        )

        self.storage.create_plan(objective_id, spec["id"], plan)

        # Activate
        self.storage.update_plan(plan_id, status="active", activated_at=_now_iso())
        self.storage.update_spec(spec["id"], status="planned")

        composer_emit(self.conductor_storage, "composer.plan_validated", "",
                      objective_id=objective_id, payload={"plan_id": plan_id})
        composer_emit(self.conductor_storage, "composer.plan_activated", "",
                      objective_id=objective_id, payload={"plan_id": plan_id})

        if self.metrics:
            self.metrics.inc("conductor_composer_plans_total")

        return {"plan_id": plan_id}

    async def _dispatch_ready(self, objective_id: str) -> list[dict]:
        spec = self.storage.get_spec_by_objective(objective_id)
        plan_dict = self.storage.get_plan_by_objective(objective_id)
        if not spec or not plan_dict:
            return []

        ctx = await self._build_context(spec, objective_id)
        if ctx is None:
            return []

        plan = self._dict_to_plan(plan_dict)
        repo_url = spec.get("repository_url", "")
        base_branch = spec.get("base_branch", "master")

        dispatched = self.scheduler.dispatch_ready_tasks(
            plan, spec, objective_id, repo_url, base_branch,
        )
        return dispatched

    # ── Reconcile ────────────────────────────────────────────────────────

    async def reconcile_objective(self, objective_id: str) -> dict:
        """Reconcile a single active objective."""
        spec = self.storage.get_spec_by_objective(objective_id)
        plan_dict = self.storage.get_plan_by_objective(objective_id)
        if not spec or not plan_dict:
            return {"error": "spec or plan not found"}

        plan = self._dict_to_plan(plan_dict)
        actions: list[str] = []

        repo_url = spec.get("repository_url", "")
        base_branch = spec.get("base_branch", "master")

        # Update task statuses from Agents Gateway
        for pt in plan_dict.get("plan_tasks", []):
            gw_task_id = pt.get("agents_gateway_task_id")
            if not gw_task_id:
                continue
            try:
                gw_task = self.agents_gateway.get_task(gw_task_id)
                new_status = self._map_gw_status(gw_task.status, gw_task.runtime_status)
                if new_status != pt["status"]:
                    branch, commit_sha = await self._extract_branch_commit_evidence(gw_task_id)
                    self.storage.update_plan_task(
                        pt["id"],
                        status=new_status,
                        branch=branch or None,
                        commit_sha=commit_sha or None,
                    )
                    actions.append(f"{pt['node_key']}: {pt['status']} -> {new_status}")

                    if new_status == "completed":
                        composer_emit(self.conductor_storage, "composer.task_completed", "",
                                      objective_id=objective_id,
                                      payload={"node_id": pt["node_key"]})
                        if self.metrics:
                            self.metrics.inc("conductor_composer_tasks_completed_total")
                    elif new_status == "waiting_for_reply":
                        composer_emit(self.conductor_storage, "composer.task_waiting_for_reply", "",
                                      objective_id=objective_id,
                                      payload={"node_id": pt["node_key"]})
                    elif new_status == "failed":
                        # Restart failed tasks with incremented attempt using proper session capture
                        attempts = pt.get("metadata", {}).get("attempt", 1)
                        new_attempt = int(attempts) + 1
                        failure_ctx = ""
                        session_id = pt.get("metadata", {}).get("session_id", "")
                        if not session_id:
                            try:
                                session = self.agents_gateway.get_task_session(gw_task_id)
                                if session:
                                    session_id = session.id
                            except Exception:
                                pass
                        if session_id:
                            try:
                                cap = self.agents_gateway.capture_session(session_id, lines=50)
                                if cap and cap.capture:
                                    failure_ctx = cap.capture
                            except Exception:
                                pass
                        node = self._find_plan_node(plan, pt.get("node_key", ""))
                        if node:
                            result = self.scheduler.restart_failed_task(
                                plan, node, spec, objective_id,
                                repo_url, base_branch,
                                failure_context=failure_ctx,
                                attempt=new_attempt,
                            )
                            if result:
                                existing_meta = pt.get("metadata", {}) or {}
                                # Detect partial creation: create_harness_task succeeded
                                # but run_task failed.  The new GW task was already
                                # cancelled — preserve both IDs for audit trail.
                                if result.get("run_failed"):
                                    attempt_history = existing_meta.get("attempt_history", [])
                                    if not isinstance(attempt_history, list):
                                        attempt_history = []
                                    attempt_history.append({
                                        "attempt": attempts,
                                        "gw_task_id": gw_task_id,
                                        "session_id": session_id or "",
                                    })
                                    partial_meta = {
                                        **existing_meta,
                                        "attempt": new_attempt,
                                        "session_id": session_id or "",
                                        "failure_context": failure_ctx[:500] if failure_ctx else "",
                                        "attempt_history": attempt_history,
                                        "partial_gw_task_id": result.get("partial_gw_task_id"),
                                        "last_restart_failed": True,
                                        "last_restart_error": "run_task_failed_after_create",
                                        "last_restart_at": _now_iso(),
                                    }
                                    self.storage.update_plan_task(
                                        pt["id"],
                                        status="blocked_external",
                                        agents_gateway_task_id=gw_task_id,
                                        metadata=partial_meta,
                                    )
                                    composer_emit(self.conductor_storage or self.storage,
                                                  "composer.task_restart_failed",
                                                  "run_task failed after create_harness_task",
                                                  objective_id=objective_id,
                                                  task_id=gw_task_id,
                                                  payload={"node_key": pt.get("node_key", ""),
                                                           "new_gw_task_id": result.get("partial_gw_task_id"),
                                                           "partial_creation": True})
                                    actions.append(f"blocked {pt['node_key']} (run_task failed after create, attempt {new_attempt})")
                                else:
                                    merged = {**existing_meta,
                                        "attempt": new_attempt,
                                        "session_id": session_id or "",
                                        "failure_context": failure_ctx[:500] if failure_ctx else "",
                                    }
                                    self.storage.update_plan_task(
                                        pt["id"],
                                        status="running",
                                        agents_gateway_task_id=result.get("gw_task_id"),
                                        metadata=merged,
                                    )
                                    actions.append(f"restarted {pt['node_key']} (attempt {new_attempt})")
                            else:
                                # Dispatch refused — mark blocked_external, preserve
                                # old GW task ID and evidence so the operator can
                                # inspect. Do NOT leave the task as permanent
                                # "failed" (which would terminally fail the
                                # objective via the completion contract).
                                existing_meta = pt.get("metadata", {}) or {}
                                failure_meta = {
                                    **existing_meta,
                                    "attempt": new_attempt,
                                    "session_id": session_id or "",
                                    "failure_context": failure_ctx[:500] if failure_ctx else "",
                                    "last_restart_failed": True,
                                    "last_restart_error": "dispatch refused — no new gw_task_id",
                                    "last_restart_at": _now_iso(),
                                }
                                self.storage.update_plan_task(
                                    pt["id"],
                                    status="blocked_external",
                                    agents_gateway_task_id=gw_task_id,
                                    metadata=failure_meta,
                                )
                                actions.append(f"blocked {pt['node_key']} (restart dispatch refused, attempt {new_attempt})")

            except Exception as exc:
                logger.warning("Reconcile failed for task %s: %s", gw_task_id, exc)

        # ── Evidence recovery pass (independent of status changes) ──
        # For every completed task with missing branch/commit_sha, repeatedly
        # attempt to recover evidence.  This is separate from the status-
        # update loop above — it runs even when status hasn't changed.
        evidence_recovered = await self._recover_missing_evidence(plan_dict, objective_id)
        if evidence_recovered:
            actions.append(f"recovered evidence for {evidence_recovered}")

        # Process interactions
        spec_dict = spec.get("normalized_spec", {})
        decisions = await self.interaction_handler.process_pending_interactions(
            objective_id, plan_dict, spec,
        )
        if decisions:
            actions.append(f"answered {len(decisions)} interactions")

        # Dispatch newly ready
        dispatched = await self._dispatch_ready(objective_id)
        if dispatched:
            actions.append(f"dispatched {len(dispatched)} tasks")
            if spec.get("status") in ("planned", "planning"):
                self.storage.update_spec(spec["id"], status="executing")

        # Check integration ready
        plan_dict = self.storage.get_plan_by_objective(objective_id)  # refreshed
        plan = self._dict_to_plan(plan_dict)
        if self.scheduler.find_integration_ready(plan):
            result = self.integration_dispatcher.dispatch_integration(
                plan, spec, objective_id, repo_url, base_branch=base_branch,
            )
            if result:
                actions.append("integration dispatched")
                if spec.get("status") == "executing":
                    self.storage.update_spec(spec["id"], status="integrating")

        # Check completion
        plan_dict = self.storage.get_plan_by_objective(objective_id)
        completion = self.verification_contract.check_completion(
            plan_dict, objective_id, agents_gateway_client=self.agents_gateway,
        )
        if completion.complete:
            await self._finalize_objective(objective_id, plan_dict, spec,
                                           verification_evidence=completion.verification_evidence)
            actions.append("objective completed")
        elif completion.blocked_external:
            self.storage.update_spec(spec["id"], status="blocked_external")
            composer_emit(self.conductor_storage, "composer.objective_blocked_external", "",
                          objective_id=objective_id,
                          payload={"reasons": completion.reasons})
            actions.append("blocked external")
        elif completion.failed:
            self.storage.update_spec(spec["id"], status="failed")
            composer_emit(self.conductor_storage, "composer.objective_failed", "",
                          objective_id=objective_id,
                          payload={"reasons": completion.reasons})
            actions.append("failed")

        return {"objective_id": objective_id, "actions": actions}

    async def _extract_branch_commit_evidence(self, gw_task_id: str) -> tuple[str, str]:
        """Extract branch and commit SHA from GW events and artifacts.

        WorktreeInfo may not carry commit_sha, so we extract from:
        1. Worktree for branch/path
        2. Task events — locate latest 'git.committed' event
        3. result.json artifact — read git.commit_sha

        Returns (branch, commit_sha) — each may be empty if not found.
        """
        branch = ""
        commit_sha = ""

        # 1. Worktree for branch/path
        try:
            wt = self.agents_gateway.get_task_worktree(gw_task_id)
            if wt:
                branch = wt.branch or ""
                commit_sha = wt.commit_sha if hasattr(wt, "commit_sha") else ""
        except Exception:
            pass

        # 2. Events — locate latest git.committed event
        if not commit_sha:
            try:
                events = self.agents_gateway.get_events(gw_task_id)  # type: ignore[union-attr]
                for evt in reversed(events):
                    etype = evt.event if hasattr(evt, "event") else evt.get("event", "")
                    edata = evt.data if hasattr(evt, "data") else evt.get("data", {})
                    if etype == "git.committed":
                        if isinstance(edata, dict):
                            commit_sha = edata.get("sha", edata.get("commit_sha", ""))
                            if not branch:
                                branch = edata.get("branch", "")
                        break
            except Exception:
                pass

        # 3. result.json artifact — read git.commit_sha
        if not commit_sha:
            try:
                raw = self.agents_gateway.download_artifact(gw_task_id, "result.json")  # type: ignore[union-attr]
                if raw:
                    result_data = json.loads(raw) if isinstance(raw, bytes) else raw
                    if isinstance(result_data, dict):
                        git_info = result_data.get("git", {})
                        if isinstance(git_info, dict):
                            commit_sha = git_info.get("commit_sha", "")
                            if not branch:
                                branch = git_info.get("branch", "")
            except Exception:
                pass

        return (branch, commit_sha)

    async def _recover_missing_evidence(self, plan_dict: dict, objective_id: str) -> int:
        """Recover missing evidence for completed tasks independently of status transitions.

        For every completed task with missing branch/commit_sha, repeatedly
        attempt to recover evidence.  This runs on every reconcile pass —
        not just when status changes — because GW events and artifacts may
        arrive with a delay.
        """
        recovered = 0
        for pt in plan_dict.get("plan_tasks", []):
            if pt.get("status") != "completed":
                continue
            gw_task_id = pt.get("agents_gateway_task_id")
            if not gw_task_id:
                continue
            branch = pt.get("branch", "") or ""
            commit_sha = pt.get("commit_sha", "") or ""
            if branch and commit_sha:
                continue
            try:
                new_branch, new_commit = await self._extract_branch_commit_evidence(gw_task_id)
            except Exception:
                continue
            updated = False
            if new_branch and not branch:
                branch = new_branch
                updated = True
            if new_commit and not commit_sha:
                commit_sha = new_commit
                updated = True
            if updated:
                self.storage.update_plan_task(
                    pt["id"],
                    branch=branch or None,
                    commit_sha=commit_sha or None,
                )
                recovered += 1
        return recovered

    def _collect_downstream_artifacts(self, plan_tasks: list[dict]) -> list[dict]:
        """Collect downstream Agents Gateway artifacts per task.

        For each task with a GW task ID, list the actual artifacts from
        GW (HTML report, result.json, terminal/session log, verification
        logs, screenshots, videos, and other proof artifacts) so the
        Composer report can reference them.
        """
        artifacts_by_task: list[dict] = []
        for pt in plan_tasks:
            gw_task_id = pt.get("agents_gateway_task_id")
            if not gw_task_id:
                continue
            node_key = pt.get("node_key", "")
            task_artifacts: list[dict] = []
            try:
                gw_arts = self.agents_gateway.get_artifacts(gw_task_id)
                for a in gw_arts:
                    task_artifacts.append({
                        "name": a.name,
                        "id": a.id,
                        "path": a.path,
                        "size_bytes": a.size_bytes,
                        "artifact_type": a.artifact_type,
                        "created_at": a.created_at,
                    })
            except Exception as exc:
                logger.debug("No artifacts for GW task %s: %s", gw_task_id, exc)

            # Also reference verification data
            try:
                verif = self.agents_gateway.get_verification(gw_task_id)
                task_artifacts.append({
                    "name": "verification",
                    "id": verif.id if hasattr(verif, "id") else "",
                    "path": "",
                    "size_bytes": 0,
                    "artifact_type": "verification",
                    "created_at": getattr(verif, "completed_at", "") if hasattr(verif, "completed_at") else "",
                })
            except Exception:
                pass

            artifacts_by_task.append({
                "node_key": node_key,
                "gw_task_id": gw_task_id,
                "artifacts": task_artifacts,
            })
        return artifacts_by_task

    def _find_plan_node(self, plan: ComposerPlan, node_id: str) -> TaskNode | None:
        for t in plan.tasks:
            if t.node_id == node_id:
                return t
        if plan.integration and plan.integration.node_id == node_id:
            # Build a TaskNode from IntegrationNode so the restart logic
            # can reuse the implementation-task path.  Goal and ownership
            # notes come straight from the durable IntegrationNode.
            return TaskNode(
                node_id=plan.integration.node_id,
                title=plan.integration.title,
                task_type="integration",
                goal=plan.integration.goal or "Integration: combine task branches and run full verification",
                ownership_notes=plan.integration.ownership_notes,
                dependencies=plan.integration.dependencies,
                harness_profile=self.config.integration_harness_profile,
                verification=plan.integration.verification,
                agents_gateway_task_id=plan.integration.agents_gateway_task_id,
                status=plan.integration.status,
                branch=plan.integration.branch,
                commit_sha=plan.integration.commit_sha,
            )
        return None

    async def _finalize_objective(self, objective_id: str, plan_dict: dict, spec: dict,
                              verification_evidence: list[dict] | None = None) -> None:
        """Generate report and mark objective complete."""
        plan_tasks = plan_dict.get("plan_tasks", [])
        integration_task = next((t for t in plan_tasks if t.get("node_key") == "integration"), None)

        final_branch = (integration_task.get("branch") or "") if integration_task else ""
        final_commit = (integration_task.get("commit_sha") or "") if integration_task else ""

        # Aggregate downstream Agents Gateway artifacts per task
        downstream_artifacts = self._collect_downstream_artifacts(plan_tasks)

        # Call final-summary LLM before generating report
        summary = None
        try:
            title = spec.get("title", "") or spec.get("raw_spec", "")[:100]
            tasks_str = str([{
                "node_id": t.get("node_key"),
                "status": t.get("status"),
                "branch": t.get("branch"),
                "commit_sha": t.get("commit_sha"),
            } for t in plan_tasks])
            interactions = self.storage.list_interaction_decisions(objective_id)
            verif_str = str(verification_evidence or [t.get("verification") for t in plan_tasks])
            summary = await self.llm.create_final_summary(
                title=title,
                status="completed",
                tasks=tasks_str,
                interactions=str(interactions)[:2000],
                verification=verif_str,
            )
        except Exception as exc:
            logger.warning("create_final_summary failed: %s", exc)

        report = self.report_generator.generate_report(
            objective_id=objective_id,
            spec=spec,
            plan=plan_dict,
            final_status="completed",
            final_branch=final_branch,
            final_commit_sha=final_commit,
            verification_results=verification_evidence or [],
            summary=summary.model_dump() if summary else None,
            downstream_artifacts=downstream_artifacts,
        )

        self.storage.update_spec(spec["id"], status="completed")
        self.storage.update_plan(plan_dict["id"], status="completed", completed_at=_now_iso())

        composer_emit(self.conductor_storage, "composer.report_generated", "",
                      objective_id=objective_id,
                      payload={"report_id": report.get("id", "")})
        composer_emit(self.conductor_storage, "composer.objective_completed", "",
                      objective_id=objective_id)

        # Update conductor objective
        try:
            self.conductor_storage.update_objective_status(objective_id, "active")
        except Exception:
            pass
        try:
            self.conductor_storage.update_objective_status(objective_id, "completed")
        except Exception:
            pass

        if self.metrics:
            self.metrics.inc("conductor_composer_objectives_completed_total")

    async def force_completion(self, objective_id: str) -> dict:
        """DEV-ONLY: drive the mock Agents Gateway tasks through completion,
        verification, integration, finalize, and report generation. Returns
        a JSON-serializable summary of what was driven.

        This endpoint is the engine behind ``scripts/e2e-composer-local.sh``
        — it lets the local end-to-end script deterministically prove
        completion, integration verification, and report+objective-completed
        state WITHOUT waiting on real agents or shell/tmux.

        Disabled in production: ComposerService refuses to run this when
        ``test_mode`` is False OR when ``MockAgentsGatewayClient`` is not
        the configured downstream. The HTTP endpoint gates twice
        (config + isinstance).
        """
        if not getattr(self.config, "test_mode", False):
            return {"error": "force_completion only available when composer.test_mode is true"}
        # Detect mock-only downstream by checking for the mock-specific
        # ``complete_task`` accepting arbitrary output strings without
        # raising. We use the simplest test: the mock client carries
        # ``set_verification`` as a public method (other clients do not).
        gw = self.agents_gateway
        if not hasattr(gw, "set_verification") or not hasattr(gw, "set_task_worktree"):
            return {"error": "force_completion only available with a mock gateway"}

        spec = self.storage.get_spec_by_objective(objective_id)
        plan_dict = self.storage.get_plan_by_objective(objective_id)
        if not spec or not plan_dict:
            return {"error": "spec or plan not found"}

        actions: list[str] = []
        # Drive each plan task that has a GW task ID to completed, set a
        # passing verification record, and set a worktree branch/commit.
        # Skip integration until all impls are completed — then run the
        # integration dispatcher through reconcile.
        for pt in plan_dict.get("plan_tasks", []):
            gw_id = pt.get("agents_gateway_task_id")
            node_key = pt.get("node_key", "")
            if not gw_id or node_key == "integration":
                continue
            try:
                gw.complete_task(gw_id, f"done {node_key}")
            except Exception as exc:
                actions.append(f"complete_task({node_key}) failed: {exc}")
                continue
            gw.set_verification(gw_id, "passed", [
                {"name": "unit tests", "command": "uv run pytest -q",
                 "passed": True, "required": True,
                 "blocked": False, "blocked_reason": "",
                 "exit_code": 0, "output_artifact": "",
                 "duration_seconds": 0.5},
            ])
            gw.set_task_worktree(gw_id,
                                 branch=f"feat/{node_key}",
                                 commit_sha=f"sha{node_key}")
            actions.append(f"completed {node_key} gw_id={gw_id}")

        # Drive integration through the integration dispatcher via
        # reconcile_objective. Integration dispatcher's check should now
        # find impls ready.
        rec = await self.reconcile_objective(objective_id)
        actions.append(f"reconcile actions: {len(rec.get('actions', []))}")

        # If integration has been dispatched through reconcile, drive
        # that to completion too and re-reconcile to finalize.
        plan_dict = self.storage.get_plan_by_objective(objective_id)
        for pt in plan_dict.get("plan_tasks", []):
            gw_id = pt.get("agents_gateway_task_id")
            if not gw_id or pt.get("node_key") != "integration":
                continue
            try:
                gw.complete_task(gw_id, "integration done")
                gw.set_verification(gw_id, "passed", [
                    {"name": "full test suite", "command": "uv run pytest -q",
                     "passed": True, "required": True,
                     "blocked": False, "blocked_reason": "",
                     "exit_code": 0, "output_artifact": "",
                     "duration_seconds": 1.0},
                ])
                gw.set_task_worktree(gw_id,
                                     branch="integration/main",
                                     commit_sha="intsha123")
                actions.append(f"completed integration gw_id={gw_id}")
            except Exception as exc:
                actions.append(f"integration complete failed: {exc}")
            break

        # The second reconcile runs finalize_objective + report +
        # objective completed.
        rec2 = await self.reconcile_objective(objective_id)
        actions.append(f"final reconcile actions: {len(rec2.get('actions', []))}")

        # Sanity: confirm spec is now completed.
        spec_after = self.storage.get_spec_by_objective(objective_id)
        final_status = spec_after.get("status", "unknown") if spec_after else "missing"
        # If still not completed (likely integration dispatcher missed),
        # try once more with a short delay.
        if final_status != "completed":
            rec3 = await self.reconcile_objective(objective_id)
            actions.append(f"third reconcile actions: {len(rec3.get('actions', []))}")
            spec_after = self.storage.get_spec_by_objective(objective_id)
            final_status = spec_after.get("status", "unknown") if spec_after else "missing"

        return {
            "objective_id": objective_id,
            "actions": actions,
            "final_status": final_status,
        }

    # ── Helpers ─────────────────────────────────────────────────────────

    def _map_gw_status(self, gw_status: str, runtime_status: str | None) -> str:
        """Map Agents Gateway task status to Composer plan task status."""
        if runtime_status:
            rs_map = {
                "completed": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
                "running": "running",
                "waiting_for_reply": "waiting_for_reply",
                "verifying": "verifying",
                "blocked_external": "blocked_external",
                "stalled": "failed",
                "created": "pending",
                "starting": "running",
            }
            return rs_map.get(runtime_status, "pending")
        return {
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "running": "running",
            "queued": "running",
            "created": "pending",
            "dispatched": "dispatching",
            "waiting": "waiting_for_reply",
        }.get(gw_status, "pending")

    def _dict_to_plan(self, plan_dict: dict) -> ComposerPlan:
        """Reconstruct a ComposerPlan from storage dict.

        Title, goal, ownership_notes, and task_type are durable SQLite
        columns now — never reconstruct these from metadata.  The metadata
        dict still carries transient fields (attempt, session_id, failure_context).
        """
        plan_tasks = plan_dict.get("plan_tasks", [])
        tasks: list[TaskNode] = []
        integration: IntegrationNode | None = None
        for pt in plan_tasks:
            verification = pt.get("verification", {})
            if isinstance(verification, dict):
                commands = [
                    VerificationCommand(**c) if isinstance(c, dict) else VerificationCommand()
                    for c in verification.get("commands", [])
                ]
                live_e2e = verification.get("live_e2e")
                vs = VerificationSpec(
                    required=verification.get("required", True),
                    commands=commands,
                    live_e2e=live_e2e,
                )
            else:
                vs = VerificationSpec()

            if pt.get("node_key") == "integration" or pt.get("task_type") == "integration":
                integration = IntegrationNode(
                    required=True,
                    node_id=pt.get("node_key", "integration"),
                    title=pt.get("title", "") or "Integrate completed task branches",
                    task_type="integration",
                    goal=pt.get("goal", "") or "Integration: combine task branches and run full verification",
                    ownership_notes=pt.get("ownership_notes", ""),
                    dependencies=pt.get("dependencies", []),
                    verification=vs,
                    status=pt.get("status", "pending"),
                    conductor_task_id=pt.get("conductor_task_id"),
                    agents_gateway_task_id=pt.get("agents_gateway_task_id"),
                    branch=pt.get("branch"),
                    commit_sha=pt.get("commit_sha"),
                )
            else:
                tasks.append(TaskNode(
                    node_id=pt.get("node_key", ""),
                    title=pt.get("title", "") or pt.get("task_type", ""),
                    task_type=pt.get("task_type", "implementation"),
                    goal=pt.get("goal", ""),
                    dependencies=pt.get("dependencies", []),
                    file_scope=pt.get("file_scope", []),
                    ownership_notes=pt.get("ownership_notes", ""),
                    harness_profile=pt.get("harness_profile", "opencode-deepseek"),
                    required_skills=pt.get("required_skills", []),
                    required_capabilities=pt.get("required_capabilities", []),
                    verification=vs,
                    conductor_task_id=pt.get("conductor_task_id"),
                    agents_gateway_task_id=pt.get("agents_gateway_task_id"),
                    status=pt.get("status", "pending"),
                    branch=pt.get("branch"),
                    commit_sha=pt.get("commit_sha"),
                    artifact_refs=pt.get("artifact_refs", []),
                    metadata=pt.get("metadata", {}),
                ))

        return ComposerPlan(
            id=plan_dict.get("id", ""),
            objective_id=plan_dict.get("objective_id", ""),
            spec_id=plan_dict.get("spec_id", ""),
            version=plan_dict.get("version", 1),
            status=plan_dict.get("status", "draft"),
            tasks=tasks,
            integration=integration,
            created_at=plan_dict.get("created_at", ""),
            activated_at=plan_dict.get("activated_at"),
            completed_at=plan_dict.get("completed_at"),
        )

    def _get_objective_status(self, objective_id: str) -> str:
        spec = self.storage.get_spec_by_objective(objective_id)
        return spec.get("status", "unknown") if spec else "unknown"

    # ── Read APIs ───────────────────────────────────────────────────────

    def get_spec(self, objective_id: str) -> dict | None:
        return self.storage.get_spec_by_objective(objective_id)

    def get_plan(self, objective_id: str) -> dict | None:
        return self.storage.get_plan_by_objective(objective_id)

    def get_tasks(self, objective_id: str) -> list[dict]:
        plan = self.storage.get_plan_by_objective(objective_id)
        if not plan:
            return []
        return plan.get("plan_tasks", [])

    def get_timeline(self, objective_id: str) -> list[dict]:
        from conductor.events import list_events
        evts = list_events(self.conductor_storage, objective_id=objective_id, limit=100)
        return [e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in evts]

    def get_report(self, objective_id: str) -> dict | None:
        return self.storage.get_report_by_objective(objective_id)

    def list_objectives(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        objs = self.conductor_storage.list_objectives(status=status, limit=limit, offset=offset)
        result: list[dict] = []
        for obj in objs:
            spec = self.storage.get_spec_by_objective(obj["id"])
            if spec:
                obj["composer_status"] = spec.get("status", "")
            result.append(obj)
        return result

    def get_objective(self, objective_id: str) -> dict | None:
        obj = self.conductor_storage.get_objective(objective_id)
        if not obj:
            return None
        spec = self.storage.get_spec_by_objective(objective_id)
        if spec:
            obj["composer_spec"] = spec
        plan = self.storage.get_plan_by_objective(objective_id)
        if plan:
            obj["composer_plan"] = plan
        return obj

    # ── Control ─────────────────────────────────────────────────────────

    async def pause_objective(self, objective_id: str) -> dict | None:
        """Pause the objective — persist previous_status and paused_at.

        Pause must:
        - prevent new dispatches (supervisor skips paused)
        - preserve active task and plan state (no cancellation)
        - record the exact prior status for exact resume
        """
        spec = self.storage.get_spec_by_objective(objective_id)
        if spec and spec.get("status") != "paused":
            from datetime import UTC, datetime
            paused_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            self.storage.update_spec(
                spec["id"],
                status="paused",
                previous_status=spec.get("status", "received"),
                paused_at=paused_at,
            )
            composer_emit(self.conductor_storage, "composer.objective_paused", "",
                          objective_id=objective_id,
                          payload={"previous_status": spec.get("status", "")})
        try:
            return self.conductor_storage.update_objective_status(objective_id, "paused")
        except Exception:
            return None

    async def resume_objective(self, objective_id: str) -> dict:
        """Resume the objective — restore exact prior state.

        Resume must:
        - restore the exact previous_status (no second plan, no normalization rerun)
        - keep the same plan and task IDs
        - continue reconciliation from where it left off

        For pre-executing states (received, normalized, planning, planned),
        start_objective advances through the pipeline idempotently without
        creating a second plan — it checks the current spec status and only
        proceeds to the next phase.

        For executing/integrating/verifying states, only reconcile is called
        — no plan or normalization is reinstated.
        """
        spec = self.storage.get_spec_by_objective(objective_id)
        prev_status = ""
        if spec and spec.get("status") == "paused":
            prev_status = spec.get("previous_status") or "executing"
            # Restore the exact prior status
            self.storage.update_spec(spec["id"], status=prev_status)
            composer_emit(self.conductor_storage, "composer.objective_resumed", "",
                          objective_id=objective_id,
                          payload={"restored_status": prev_status})
        else:
            prev_status = spec.get("status", "") if spec else ""
        try:
            self.conductor_storage.update_objective_status(objective_id, "active")
        except Exception:
            pass

        # Resume to executing/integrating/verifying/dispatching: just reconcile,
        # no normalization/planning rerun.  The plan and task IDs are already
        # in SQLite from the pause.
        if prev_status in ("executing", "integrating", "verifying", "dispatching"):
            return await self.reconcile_objective(objective_id)

        # Resume to pre-executing states: start_objective advances idempotently.
        # For received -> normalize.  For.normalized -> plan.  For planned -> dispatch.
        # If a plan already exists, _create_and_activate_plan checks spec.status
        # == "normalized" and won't create a second plan.
        if prev_status in ("received", "normalizing", "normalized", "planning", "planned"):
            return await self.start_objective(objective_id)

        return {"objective_id": objective_id, "status": self._get_objective_status(objective_id)}

    async def cancel_objective(self, objective_id: str) -> dict | None:
        spec = self.storage.get_spec_by_objective(objective_id)
        if spec:
            self.storage.update_spec(spec["id"], status="cancelled")
            # Cancel active Agents Gateway tasks
            plan_dict = self.storage.get_plan_by_objective(objective_id)
            if plan_dict:
                for pt in plan_dict.get("plan_tasks", []):
                    gw_id = pt.get("agents_gateway_task_id")
                    if gw_id and pt.get("status") in ("running", "dispatching", "pending", "verifying", "waiting_for_reply"):
                        try:
                            self.agents_gateway.cancel_task(gw_id)
                            self.storage.update_plan_task(pt["id"], status="cancelled")
                        except Exception as exc:
                            logger.warning("Failed to cancel GW task %s: %s", gw_id, exc)
        try:
            return self.conductor_storage.update_objective_status(objective_id, "cancelled")
        except Exception:
            return None

    async def steer_objective(self, objective_id: str, guidance: str) -> dict:
        """Add steering guidance to an active objective.
        
        Steered guidance is persisted and will be included in later planning,
        task, and interaction context.
        """
        from conductor.events import emit
        emit(self.conductor_storage, "composer.objective_steered", guidance,
             objective_id=objective_id, source="user")
        # Persist steering in spec metadata
        spec = self.storage.get_spec_by_objective(objective_id)
        if spec:
            existing_meta = spec.get("metadata") if "metadata" in spec else {}
            if not isinstance(existing_meta, dict):
                existing_meta = {}
            steering_list = existing_meta.get("steering", [])
            if not isinstance(steering_list, list):
                steering_list = []
            steering_list.append({"guidance": guidance, "at": _now_iso()})
            # Store steering on the raw_spec's metadata in normalized_spec if available,
            # otherwise on the spec's internal metadata
            ns = spec.get("normalized_spec", {}) or {}
            if isinstance(ns, dict):
                constraints = ns.get("constraints", [])
                if not isinstance(constraints, list):
                    constraints = []
                constraints.append(f"Steering: {guidance}")
                ns["constraints"] = constraints
                self.storage.update_spec(spec["id"], normalized_spec=ns)
        return {"objective_id": objective_id, "steered": True}
