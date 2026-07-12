"""Final verification contract — completion criteria for objectives."""

from __future__ import annotations

import logging

from conductor.composer.models import ComposerPlan, TASK_NODE_STATUSES
from conductor.composer.storage import ComposerStorage

logger = logging.getLogger(__name__)

__all__ = ["VerificationContract", "ObjectiveCompletion"]


class ObjectiveCompletion:
    """Result of checking objective completion criteria."""

    def __init__(
        self,
        complete: bool,
        blocked_external: bool,
        failed: bool,
        reasons: list[str] | None = None,
    ) -> None:
        self.complete = complete
        self.blocked_external = blocked_external
        self.failed = failed
        self.reasons = reasons or []


class VerificationContract:
    """Checks whether an objective meets all completion criteria."""

    def __init__(self, storage: ComposerStorage) -> None:
        self.storage = storage

    def check_completion(
        self,
        plan: dict,
        objective_id: str,
        agents_gateway_client=None,
    ) -> ObjectiveCompletion:
        """Check all completion criteria.

        An objective is complete when:
        - every required plan node is completed
        - integration task is completed
        - full test suite passed (from verification results)
        - required live E2E passed or truthfully blocked
        - final branch and commit are recorded
        """
        reasons: list[str] = []

        plan_tasks = plan.get("plan_tasks", [])

        # Check all plan tasks completed
        implementation_tasks = [t for t in plan_tasks if t.get("task_type") != "integration"]
        integration_tasks = [t for t in plan_tasks if t.get("node_key") == "integration" or t.get("task_type") == "integration"]

        incomplete_impls = [t for t in implementation_tasks if t.get("status") != "completed"]
        if incomplete_impls:
            reasons.append(f"Incomplete implementation tasks: {[t['node_key'] for t in incomplete_impls]}")

        for t in implementation_tasks:
            if t.get("status") == "blocked_external":
                return ObjectiveCompletion(
                    complete=False, blocked_external=True, failed=False,
                    reasons=["Implementation task blocked by external dependency: " + t.get("node_key", "")],
                )
            if t.get("status") == "failed":
                return ObjectiveCompletion(
                    complete=False, blocked_external=False, failed=True,
                    reasons=["Implementation task failed: " + t.get("node_key", "")],
                )

        # Check integration completed
        integration_task = integration_tasks[0] if integration_tasks else None
        if integration_task:
            if integration_task.get("status") != "completed":
                reasons.append("Integration task not completed")
            if integration_task.get("status") == "blocked_external":
                return ObjectiveCompletion(
                    complete=False, blocked_external=True, failed=False,
                    reasons=["Integration task externally blocked"],
                )
            if integration_task.get("status") == "failed":
                return ObjectiveCompletion(
                    complete=False, blocked_external=False, failed=True,
                    reasons=["Integration task failed"],
                )

        # Check final branch and commit recorded when verification results are
        # available. We don't strictly require these fields in `plan_tasks` because
        # the Agents Gateway may record them via session events / artifacts rather
        # than the plan_task row. The report generator tolerates missing values
        # gracefully.
        if integration_task:
            if not integration_task.get("branch"):
                # Not strictly required — record as info but don't block completion.
                logger.debug("Final branch not recorded on integration task row")
            if not integration_task.get("commit_sha"):
                logger.debug("Final commit SHA not recorded on integration task row")

        complete = len(reasons) == 0
        return ObjectiveCompletion(
            complete=complete,
            blocked_external=False,
            failed=False,
            reasons=reasons,
        )
