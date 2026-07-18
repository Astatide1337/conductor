"""Integration task — combines completed task branches into a final branch."""

from __future__ import annotations

import logging

from conductor.composer.events import composer_emit
from conductor.composer.goals import build_integration_brief
from conductor.composer.models import ComposerPlan, IntegrationNode, TaskNode, VerificationSpec, VerificationCommand
from conductor.composer.scheduler import build_idempotency_key
from conductor.composer.storage import ComposerStorage

logger = logging.getLogger(__name__)

__all__ = ["IntegrationDispatcher"]


class IntegrationDispatcher:
    """Creates and dispatches the integration task once all dependencies complete."""

    def __init__(
        self,
        storage: ComposerStorage,
        agents_gateway_client,
        *,
        integration_harness_profile: str = "opencode-deepseek",
        base_branch: str = "master",
        metrics=None,
        conductor_storage=None,
    ) -> None:
        self.storage = storage
        self.agents_gateway = agents_gateway_client
        self.integration_profile = integration_harness_profile
        self.base_branch = base_branch
        self.metrics = metrics
        self.conductor_storage = conductor_storage

    def dispatch_integration(
        self,
        plan: ComposerPlan,
        spec: dict,
        objective_id: str,
        repo_url: str = "",
        base_branch: str | None = None,
    ) -> dict | None:
        """Create the integration task node and dispatch it."""
        if not plan.integration:
            return None

        # Use caller-provided base_branch if given; otherwise fall back to constructor default
        effective_branch = base_branch if base_branch is not None else self.base_branch

        completed_tasks = [t for t in plan.tasks if t.status == "completed"]

        composer_emit(self.conductor_storage or self.storage, "composer.integration_ready", "",
                      objective_id=objective_id,
                      payload={"plan_id": plan.id,
                               "completed_nodes": [t.node_id for t in completed_tasks]})

        from conductor.composer.models import NormalizedSpec
        ns_dict = spec.get("normalized_spec", {})
        ns = NormalizedSpec(**ns_dict) if isinstance(ns_dict, dict) else NormalizedSpec()

        integration_goal = build_integration_brief(
            spec=ns,
            completed_tasks=completed_tasks,
            integration_profile=self.integration_profile,
            base_branch=effective_branch,
        )

        # Check harness availability
        try:
            availability = self.agents_gateway.check_harness_availability(self.integration_profile)
            if not availability.runnable:
                composer_emit(self.conductor_storage or self.storage, "composer.task_blocked_external", availability.error,
                              objective_id=objective_id,
                              payload={"node_id": "integration"})
                return None
        except Exception as exc:
            logger.warning("Integration harness availability check failed: %s", exc)

        task_spec = {
            "objective_id": objective_id,
            "composer_task_id": f"plan_task_integration",
            "title": "Integration: combine task branches and run full verification",
            "brief": integration_goal,
            "repo": {
                "url": repo_url,
                "base_branch": effective_branch,
            },
            "execution": {
                "mode": "harness_session",
                "runtime": "tmux",
                "isolation": "worktree",
                "harness_profile": self.integration_profile,
            },
            "goal": {
                "strategy": "auto",
                "text": integration_goal,
            },
            "required_skills": [],
            "required_tools": [],
            "verification": plan.integration.verification.model_dump(),
            "artifacts": {
                "html_report": True,
                "terminal_capture": True,
                "screenshots": True,
                "videos": True,
            },
            "metadata": {
                "composer_objective_id": objective_id,
                "composer_plan_id": plan.id,
                "composer_node_id": "integration",
                "dependency_branches": [
                    {"node_id": t.node_id, "branch": t.branch or "", "commit_sha": t.commit_sha or ""}
                    for t in completed_tasks
                ],
            },
        }

        idem_key = build_idempotency_key(objective_id, plan.id, "integration", 1)

        try:
            gw_task = self.agents_gateway.create_harness_task(task_spec, idempotency_key=idem_key)

            # Update plan task row for integration
            pt = self.storage.get_plan_task_by_node(plan.id, "integration")
            if pt:
                self.storage.update_plan_task(
                    pt["id"],
                    agents_gateway_task_id=gw_task.id,
                    status="dispatching",
                )

            self.agents_gateway.run_task(gw_task.id)

            if pt:
                self.storage.update_plan_task(pt["id"], status="running")

            # Update plan status
            self.storage.update_plan(plan.id, status="integrating")

            composer_emit(self.conductor_storage or self.storage, "composer.integration_dispatched", "",
                          objective_id=objective_id,
                          payload={"gw_task_id": gw_task.id})

            return {"node_id": "integration", "gw_task_id": gw_task.id}

        except Exception as exc:
            logger.error("Failed to dispatch integration task: %s", exc)
            composer_emit(self.conductor_storage or self.storage, "composer.task_blocked_external", str(exc),
                          objective_id=objective_id,
                          payload={"node_id": "integration"})
            return None
