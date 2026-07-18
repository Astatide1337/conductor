"""Task graph scheduler — dependency-aware dispatch for Composer."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from conductor.composer.events import composer_emit
from conductor.composer.goals import build_task_brief
from conductor.composer.models import ComposerPlan, TaskNode
from conductor.composer.storage import ComposerStorage

logger = logging.getLogger(__name__)

__all__ = ["Scheduler", "build_idempotency_key"]


def build_idempotency_key(objective_id: str, plan_id: str, node_id: str, attempt: int = 1) -> str:
    return f"composer:{objective_id}:{plan_id}:{node_id}:{attempt}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Scheduler:
    """Dependency and concurrency-aware task dispatch.

    Finds ready nodes, validates capabilities and harness availability,
    and dispatches through Agents Gateway.  Does not dispatch two
    nodes with explicitly conflicting ownership simultaneously.
    """

    def __init__(
        self,
        storage: ComposerStorage,
        agents_gateway_client,
        *,
        max_parallel_tasks: int = 3,
        metrics=None,
        conductor_storage=None,
    ) -> None:
        self.storage = storage
        self.agents_gateway = agents_gateway_client
        self.max_parallel_tasks = max_parallel_tasks
        self.metrics = metrics
        self.conductor_storage = conductor_storage

    def find_ready_nodes(self, plan: ComposerPlan) -> list[TaskNode]:
        """Find pending nodes whose dependencies are all completed."""
        completed_ids: set[str] = {
            t.node_id for t in plan.tasks if t.status == "completed"
        }
        if plan.integration and plan.integration.status == "completed":
            completed_ids.add(plan.integration.node_id)

        ready: list[TaskNode] = []
        for node in plan.tasks:
            if node.status != "pending":
                continue
            deps_met = all(d in completed_ids for d in node.dependencies)
            if deps_met:
                ready.append(node)

        return ready

    def find_integration_ready(self, plan: ComposerPlan) -> bool:
        """Check if integration can start."""
        if not plan.integration or not plan.integration.required:
            return False
        if plan.integration.status not in ("pending",):
            return False
        completed_ids = {t.node_id for t in plan.tasks if t.status == "completed"}
        return all(d in completed_ids for d in plan.integration.dependencies)

    def dispatch_ready_tasks(
        self,
        plan: ComposerPlan,
        spec: dict,
        objective_id: str,
        repo_url: str = "",
        base_branch: str = "master",
    ) -> list[dict]:
        """Dispatch up to ``max_parallel_tasks`` ready tasks."""
        ready = self.find_ready_nodes(plan)
        if not ready:
            return []

        running = sum(1 for t in plan.tasks if t.status in ("dispatching", "running", "waiting_for_reply", "verifying"))
        slots = self.max_parallel_tasks - running
        if slots <= 0:
            return []

        to_dispatch = self._filter_conflicts(ready, plan)
        to_dispatch = to_dispatch[:slots]

        dispatched: list[dict] = []
        for node in to_dispatch:
            try:
                availability = self.agents_gateway.check_harness_availability(node.harness_profile)
                if not availability.runnable:
                    self._mark_blocked(plan, node, availability.error or "harness not runnable")
                    continue
            except Exception as exc:
                logger.warning("Harness availability check failed for %s: %s", node.harness_profile, exc)

            attempt = 1
            result = self._dispatch_one(node, spec, objective_id, plan.id, repo_url, base_branch, attempt)
            if result:
                dispatched.append(result)

        return dispatched

    def _dispatch_one(
        self,
        node: TaskNode,
        spec: dict,
        objective_id: str,
        plan_id: str,
        repo_url: str,
        base_branch: str,
        attempt: int,
    ) -> dict | None:
        """Dispatch a single task node through Agents Gateway."""
        composer_emit(self.conductor_storage or self.storage, "composer.task_dispatching", "",
                      objective_id=objective_id,
                      payload={"node_id": node.node_id, "plan_id": plan_id})

        # Build full task brief with dependency branches/commits/verification
        from conductor.composer.models import NormalizedSpec
        ns_dict = spec.get("normalized_spec", {})
        ns = NormalizedSpec(**ns_dict) if isinstance(ns_dict, dict) else NormalizedSpec()

        completed_deps: list[TaskNode] = []
        for dep_id in node.dependencies:
            dep_pt = self.storage.get_plan_task_by_node(plan_id, dep_id)
            if dep_pt and dep_pt.get("status") == "completed":
                completed_deps.append(TaskNode(
                    node_id=dep_id,
                    title=dep_pt.get("task_type", dep_id),
                    branch=dep_pt.get("branch"),
                    commit_sha=dep_pt.get("commit_sha"),
                ))

        brief_text = build_task_brief(
            node=node,
            spec=ns,
            completed_deps=completed_deps,
            overall_summary=spec.get("raw_spec", "")[:500],
        )

        idem_key = build_idempotency_key(objective_id, plan_id, node.node_id, attempt)

        task_spec = {
            "objective_id": objective_id,
            "composer_task_id": f"plan_task_{node.node_id}",
            "title": node.title,
            "brief": brief_text,
            "repo": {
                "url": repo_url,
                "base_branch": base_branch,
            },
            "execution": {
                "mode": "harness_session",
                "runtime": "tmux",
                "isolation": "worktree",
                "harness_profile": node.harness_profile,
            },
            "goal": {
                "strategy": "auto",
                "text": brief_text,
            },
            "required_skills": node.required_skills,
            "required_tools": node.required_capabilities,
            "verification": node.verification.model_dump(),
            "artifacts": {
                "html_report": True,
                "terminal_capture": True,
                "screenshots": True,
                "videos": True,
            },
            "metadata": {
                "composer_objective_id": objective_id,
                "composer_plan_id": plan_id,
                "composer_node_id": node.node_id,
                "dependency_branches": [
                    {"node_id": d.node_id, "branch": d.branch or "", "commit_sha": d.commit_sha or ""}
                    for d in completed_deps
                ],
            },
        }

        try:
            gw_task = self.agents_gateway.create_harness_task(task_spec, idempotency_key=idem_key)

            pt = self.storage.get_plan_task_by_node(plan_id, node.node_id)
            if pt:
                self.storage.update_plan_task(
                    pt["id"],
                    agents_gateway_task_id=gw_task.id,
                    status="dispatching",
                )

            try:
                self.agents_gateway.run_task(gw_task.id)
            except Exception as run_exc:
                # create_harness_task succeeded, run_task failed —
                # attempt to cancel the phantom task, persist both
                # old and new GW IDs in evidence, emit restart_failed.
                logger.error("run_task failed for new GW task %s (node %s): %s",
                             gw_task.id, node.node_id, run_exc)
                try:
                    self.agents_gateway.cancel_task(gw_task.id)
                except Exception:
                    pass
                composer_emit(self.conductor_storage or self.storage,
                              "composer.task_restart_failed",
                              f"run_task_failed: {run_exc}",
                              objective_id=objective_id,
                              payload={"node_id": node.node_id,
                                       "attempt": attempt,
                                       "new_gw_task_id": gw_task.id,
                                       "partial_creation": True})
                return {
                    "node_id": node.node_id,
                    "gw_task_id": None,
                    "partial_gw_task_id": gw_task.id,
                    "run_failed": True,
                }

            if pt:
                self.storage.update_plan_task(pt["id"], status="running")

            composer_emit(self.conductor_storage or self.storage, "composer.task_dispatched", "",
                          objective_id=objective_id,
                          payload={"node_id": node.node_id, "gw_task_id": gw_task.id})

            # Emit task_restarted only for non-attempt-1 dispatches that
            # successfully started (the first dispatch already emitted
            # composer.task_dispatching → composer.task_dispatched).
            if attempt > 1:
                composer_emit(self.conductor_storage or self.storage, "composer.task_restarted", "",
                              objective_id=objective_id,
                              payload={"node_id": node.node_id, "attempt": attempt,
                                       "gw_task_id": gw_task.id})

            if self.metrics:
                self.metrics.inc("conductor_composer_tasks_dispatched_total")

            return {"node_id": node.node_id, "gw_task_id": gw_task.id}

        except Exception as exc:
            logger.error("Failed to dispatch node %s: %s", node.node_id, exc)
            composer_emit(self.conductor_storage or self.storage, "composer.task_blocked_external", str(exc),
                           objective_id=objective_id,
                           payload={"node_id": node.node_id})
            pt = self.storage.get_plan_task_by_node(plan_id, node.node_id)
            if pt:
                self.storage.update_plan_task(pt["id"], status="blocked_external")
            return None

    def _filter_conflicts(self, ready: list[TaskNode], plan: ComposerPlan) -> list[TaskNode]:
        """Filter out tasks with conflicting file scopes."""
        running_scopes: set[str] = set()
        for t in plan.tasks:
            if t.status in ("dispatching", "running", "waiting_for_reply", "verifying"):
                running_scopes.update(t.file_scope)

        result: list[TaskNode] = []
        used_scopes = set(running_scopes)
        for node in ready:
            if node.file_scope:
                conflict = used_scopes & set(node.file_scope)
                if conflict:
                    logger.info("Skipping %s due to file scope conflict: %s", node.node_id, conflict)
                    continue
                used_scopes.update(node.file_scope)
            result.append(node)
        return result

    def _mark_blocked(self, plan: ComposerPlan, node: TaskNode, reason: str) -> None:
        pt = self.storage.get_plan_task_by_node(plan.id, node.node_id)
        if pt:
            self.storage.update_plan_task(pt["id"], status="blocked_external")
            composer_emit(self.conductor_storage or self.storage, "composer.task_blocked_external", reason,
                          objective_id=plan.objective_id,
                          payload={"node_id": node.node_id})

    def restart_failed_task(
        self,
        plan: ComposerPlan,
        node: TaskNode,
        spec: dict,
        objective_id: str,
        repo_url: str,
        base_branch: str,
        failure_context: str = "",
        attempt: int = 2,
    ) -> dict | None:
        """Restart a failed task with failure context appended to the dispatched brief.

        The original ``node.goal`` is never mutated — failure context is
        injected into a per-dispatch copy so that the durable SQLite
        ``goal`` column stays the exact planned text.

        ``composer.task_restarted`` is emitted by the dispatch layer
        only after ``run_task`` succeeds — never before the agent starts.
        """
        from copy import deepcopy
        dispatched_node = deepcopy(node)
        if failure_context:
            dispatched_node.goal = (
                f"{node.goal}\n\nPrevious attempt failed with: "
                f"{failure_context}\nPlease diagnose and continue working."
            )

        if self.metrics:
            self.metrics.inc("conductor_composer_task_restarts_total")

        return self._dispatch_one(dispatched_node, spec, objective_id, plan.id, repo_url, base_branch, attempt)
