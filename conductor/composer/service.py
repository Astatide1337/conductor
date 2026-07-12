"""Composer service — high-level API for the Composer engine.

Coordinates normalization, context building, planning, scheduling,
supervision, interactions, integration, verification, and reports.
"""

from __future__ import annotations

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
        gateway_registry=None,
        metrics=None,
    ) -> None:
        self.storage = storage
        self.conductor_storage = conductor_storage
        self.llm = llm_client
        self.agents_gateway = agents_gateway_client
        self.skills_gateway = skills_gateway_client
        self.wiki_mcp = wiki_mcp_client
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
        """Submit a finalized specification.  Creates Conductor objective + Composer spec."""
        # Create Conductor objective
        obj_id = f"obj_{_uid()}"
        now = _now_iso()
        obj = self.conductor_storage.create_objective(
            title=title,
            description=raw_spec[:500],
            created_by="composer",
            metadata={"composer": True, "repository": repository or {}},
        )
        obj_id = obj["id"]

        # Create a run
        run = self.conductor_storage.create_run(
            obj_id, planner_mode="composer", metadata={"composer": True},
        )

        # Create Composer spec
        spec = self.storage.create_spec(obj_id, title, raw_spec)
        spec_id = spec["id"]

        composer_emit(self.conductor_storage, "composer.objective_received", title,
                      objective_id=obj_id, payload={"spec_id": spec_id})

        if auto_start and self.config.auto_start:
            await self.start_objective(obj_id)

        return {
            "objective_id": obj_id,
            "composer_spec_id": spec_id,
            "status": "received",
        }

    # ── Objective lifecycle ─────────────────────────────────────────────

    async def start_objective(self, objective_id: str) -> dict:
        """Start the Composer pipeline for an objective."""
        spec = self.storage.get_spec_by_objective(objective_id)
        if not spec:
            return {"error": "spec not found", "objective_id": objective_id}

        # Normalize
        await self._normalize_spec(spec)
        # Build context
        await self._build_context(spec, objective_id)
        # Plan
        await self._create_and_activate_plan(spec, objective_id)
        # Dispatch
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
        normalized = NormalizedSpec(
            goal=result.goal,
            repository=SpecRepository(**(result.repository if isinstance(result.repository, dict) else {})),
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
        )

        composer_emit(self.conductor_storage, "composer.context_built", "",
                      objective_id=objective_id,
                      payload={"harness_count": len(ctx.harness_profiles),
                               "skill_count": len(ctx.skills)})
        return ctx

    async def _create_and_activate_plan(self, spec: dict, objective_id: str) -> dict | None:
        # Build context for planning
        ctx = await self._build_context(spec, objective_id)
        if ctx is None:
            return None

        context_str = context_to_prompt(ctx)
        spec_str = str(spec.get("normalized_spec", {}))

        try:
            plan_result = await self.llm.create_plan(spec=spec_str, context=context_str)
        except LLMError as exc:
            logger.error("LLM planning failed: %s", exc)
            if self.metrics:
                self.metrics.inc("conductor_composer_llm_errors_total")
            self.storage.update_spec(spec["id"], status="blocked_external")
            return None

        composer_emit(self.conductor_storage, "composer.plan_generated", "",
                      objective_id=objective_id,
                      payload={"task_count": len(plan_result.tasks)})

        # Validate plan
        validation = validate_plan_result(plan_result, ctx)
        if not validation.valid:
            composer_emit(self.conductor_storage, "composer.plan_validation_failed",
                          "; ".join(validation.errors),
                          objective_id=objective_id,
                          payload={"errors": validation.errors})
            # TODO: repair loop — for now block
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
        repo_info = spec.get("normalized_spec", {}).get("repository", {})
        repo_url = repo_info.get("url", "") if isinstance(repo_info, dict) else ""
        base_branch = repo_info.get("base_branch", "master") if isinstance(repo_info, dict) else "master"

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

        # Update task statuses from Agents Gateway
        for pt in plan_dict.get("plan_tasks", []):
            gw_task_id = pt.get("agents_gateway_task_id")
            if not gw_task_id:
                continue
            try:
                gw_task = self.agents_gateway.get_task(gw_task_id)
                new_status = self._map_gw_status(gw_task.status, gw_task.runtime_status)
                if new_status != pt["status"]:
                    branch = None
                    commit_sha = None
                    try:
                        wt = self.agents_gateway.get_task_worktree(gw_task_id)
                        if wt:
                            branch = wt.branch
                            commit_sha = wt.commit_sha if hasattr(wt, "commit_sha") else None
                    except Exception:
                        pass
                    self.storage.update_plan_task(
                        pt["id"],
                        status=new_status,
                        branch=branch,
                        commit_sha=commit_sha,
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

            except Exception as exc:
                logger.warning("Reconcile failed for task %s: %s", gw_task_id, exc)

        # Process interactions
        spec_dict = spec["normalized_spec"]
        decisions = await self.interaction_handler.process_pending_interactions(
            objective_id, plan_dict, spec,
        )
        if decisions:
            actions.append(f"answered {len(decisions)} interactions")

        # Dispatch newly ready
        dispatched = await self._dispatch_ready(objective_id)
        if dispatched:
            actions.append(f"dispatched {len(dispatched)} tasks")
            # Move spec from planning/planned → executing once we dispatch real tasks
            if spec.get("status") in ("planned", "planning"):
                self.storage.update_spec(spec["id"], status="executing")

        # Check integration ready
        plan_dict = self.storage.get_plan_by_objective(objective_id)  # refreshed
        plan = self._dict_to_plan(plan_dict)
        if self.scheduler.find_integration_ready(plan):
            repo_info = spec_dict.get("repository", {})
            repo_url = repo_info.get("url", "") if isinstance(repo_info, dict) else ""
            base_branch = repo_info.get("base_branch", "master") if isinstance(repo_info, dict) else "master"
            result = self.integration_dispatcher.dispatch_integration(
                plan, spec, objective_id, repo_url,
            )
            if result:
                actions.append("integration dispatched")
                if spec.get("status") == "executing":
                    self.storage.update_spec(spec["id"], status="integrating")

        # Check completion
        plan_dict = self.storage.get_plan_by_objective(objective_id)
        completion = self.verification_contract.check_completion(plan_dict, objective_id)
        if completion.complete:
            await self._finalize_objective(objective_id, plan_dict, spec)
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

    async def _finalize_objective(self, objective_id: str, plan_dict: dict, spec: dict) -> None:
        """Generate report and mark objective complete."""
        plan_tasks = plan_dict.get("plan_tasks", [])
        integration_task = next((t for t in plan_tasks if t.get("node_key") == "integration"), None)

        final_branch = (integration_task.get("branch") or "") if integration_task else ""
        final_commit = (integration_task.get("commit_sha") or "") if integration_task else ""

        report = self.report_generator.generate_report(
            objective_id=objective_id,
            spec=spec,
            plan=plan_dict,
            final_status="completed",
            final_branch=final_branch,
            final_commit_sha=final_commit,
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
            self.conductor_storage.update_objective_status(objective_id, "active")  # may already be active
        except Exception:
            pass
        try:
            self.conductor_storage.update_objective_status(objective_id, "completed")
        except Exception:
            pass

        if self.metrics:
            self.metrics.inc("conductor_composer_objectives_completed_total")

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
                "stalled": "blocked_external",
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
        """Reconstruct a ComposerPlan from storage dict."""
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
                vs = VerificationSpec(
                    required=verification.get("required", True),
                    commands=commands,
                )
            else:
                vs = VerificationSpec()

            if pt.get("node_key") == "integration" or pt.get("task_type") == "integration":
                integration = IntegrationNode(
                    required=True,
                    node_id=pt.get("node_key", "integration"),
                    title=pt.get("task_type", "integration"),
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
                    title=pt.get("task_type", ""),
                    task_type=pt.get("task_type", "implementation"),
                    goal=pt.get("metadata", {}).get("goal", ""),
                    dependencies=pt.get("dependencies", []),
                    file_scope=pt.get("file_scope", []),
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
        spec = self.storage.get_spec_by_objective(objective_id)
        if spec:
            self.storage.update_spec(spec["id"], status="cancelled")
        try:
            return self.conductor_storage.update_objective_status(objective_id, "paused")
        except Exception:
            return None

    async def resume_objective(self, objective_id: str) -> dict:
        return await self.start_objective(objective_id)

    async def cancel_objective(self, objective_id: str) -> dict | None:
        spec = self.storage.get_spec_by_objective(objective_id)
        if spec:
            self.storage.update_spec(spec["id"], status="cancelled")
        try:
            return self.conductor_storage.update_objective_status(objective_id, "cancelled")
        except Exception:
            return None

    async def steer_objective(self, objective_id: str, guidance: str) -> dict:
        """Add steering guidance to an active objective."""
        from conductor.events import emit
        emit(self.conductor_storage, "composer.objective_steered", guidance,
             objective_id=objective_id, source="user")
        return {"objective_id": objective_id, "steered": True}
