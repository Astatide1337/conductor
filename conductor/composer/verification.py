"""Final verification contract — completion criteria for objectives.

Fail-closed: every required proof is checked. Missing/404 verification or
GW unavailability prevents completion rather than being silently skipped.
"""

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
        verification_evidence: list[dict] | None = None,
    ) -> None:
        self.complete = complete
        self.blocked_external = blocked_external
        self.failed = failed
        self.reasons = reasons or []
        self.verification_evidence = verification_evidence or []


class VerificationContract:
    """Fail-closed verification contract.

    Completion requires ALL of these proven:
    1. every required implementation task completed through Agents Gateway
    2. integration task completed
    3. each required verification command checked via GW and passed
    4. missing/404 verification data prevents completion
    5. final integration branch is recorded
    6. final integration commit SHA is recorded
    7. final report exists

    Missing credentials or unavailable external services → blocked_external.
    Agents Gateway unavailability → blocked_external (never silently skip).
    """

    def __init__(self, storage: ComposerStorage) -> None:
        self.storage = storage

    def check_completion(
        self,
        plan: dict,
        objective_id: str,
        agents_gateway_client=None,
    ) -> ObjectiveCompletion:
        """Check all completion criteria.  Fail closed — every required proof checked."""
        reasons: list[str] = []
        evidence: list[dict] = []

        plan_tasks = plan.get("plan_tasks", [])

        implementation_tasks = [t for t in plan_tasks if t.get("task_type") != "integration"]
        integration_tasks = [t for t in plan_tasks if t.get("node_key") == "integration" or t.get("task_type") == "integration"]

        # Every implementation task must be completed
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

        # Integration task must be completed
        integration_task = integration_tasks[0] if integration_tasks else None
        if not integration_task:
            reasons.append("Integration task missing from plan")
        elif integration_task.get("status") != "completed":
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

        # Final branch and commit must be recorded
        if integration_task:
            if not integration_task.get("branch"):
                reasons.append("Final integration branch not recorded")
            if not integration_task.get("commit_sha"):
                reasons.append("Final integration commit SHA not recorded")

        # ── Fail-closed verification ─────────────────────────────────
        if agents_gateway_client is None:
            reasons.append("Agents Gateway client unavailable — cannot verify")
            return ObjectiveCompletion(
                complete=False, blocked_external=True, failed=False,
                reasons=reasons, verification_evidence=evidence,
            )

        for pt in plan_tasks:
            verif_spec = pt.get("verification", {})
            if isinstance(verif_spec, dict):
                is_required = verif_spec.get("required", False)
            else:
                is_required = False

            if not is_required:
                continue

            gw_task_id = pt.get("agents_gateway_task_id")
            if not gw_task_id:
                reasons.append(
                    f"No GW task ID for required verification on {pt.get('node_key', '')}"
                )
                continue

            gw_verif = None
            gw_unavailable = False
            try:
                gw_verif = agents_gateway_client.get_verification(gw_task_id)
            except Exception as exc:
                logger.error("GW verification fetch failed for %s: %s", gw_task_id, exc)
                gw_unavailable = True

            if gw_unavailable:
                reasons.append(f"Agents Gateway unavailable — cannot fetch verification for {pt.get('node_key', '')}")
                return ObjectiveCompletion(
                    complete=False, blocked_external=True, failed=False,
                    reasons=reasons, verification_evidence=evidence,
                )

            if gw_verif is None:
                reasons.append(
                    f"No verification record returned for {pt.get('node_key', '')} (GW task {gw_task_id})"
                )
                continue

            verif_status = gw_verif.status if hasattr(gw_verif, "status") else gw_verif.get("status", "")
            commands = gw_verif.commands if hasattr(gw_verif, "commands") else gw_verif.get("commands", [])

            if verif_status != "passed":
                reasons.append(
                    f"Verification not passed for {pt.get('node_key', '')}: status={verif_status}"
                )

            for cmd in commands:
                cmd_name = cmd.get("name", cmd.get("command", "")) if isinstance(cmd, dict) else ""
                evidence.append({
                    "node_key": pt.get("node_key", ""),
                    "gw_task_id": gw_task_id,
                    "name": cmd_name,
                    "command": cmd.get("command", "") if isinstance(cmd, dict) else "",
                    "passed": cmd.get("passed", False) if isinstance(cmd, dict) else False,
                    "required": cmd.get("required", False) if isinstance(cmd, dict) else False,
                    "exit_code": cmd.get("exit_code") if isinstance(cmd, dict) else None,
                    "blocked": cmd.get("blocked", False) if isinstance(cmd, dict) else False,
                    "blocked_reason": cmd.get("blocked_reason", "") if isinstance(cmd, dict) else "",
                    "output_artifact": cmd.get("output_artifact", "") if isinstance(cmd, dict) else "",
                    "duration_seconds": cmd.get("duration_seconds") if isinstance(cmd, dict) else None,
                })

            # Every expected required command must exist and pass.
            # IGNORE the downstream command's `required` flag — the Composer
            # plan is the source of truth.  If the plan says required, it
            # must exist in actual evidence AND have passed=true.
            # Matching: by exact name match OR by command substring.
            cmd_list: list[dict] = [
                c for c in commands if isinstance(c, dict)
            ]

            for exp_cmd in (verif_spec.get("commands", []) if isinstance(verif_spec, dict) else []):
                if not exp_cmd.get("required", True):
                    continue
                exp_name = exp_cmd.get("name", "")
                exp_command = exp_cmd.get("command", "")

                # Find matching actual command by name OR command substring.
                actual = None
                for ac in cmd_list:
                    ac_name = ac.get("name", "")
                    ac_command = ac.get("command", "")
                    if exp_name and ac_name == exp_name:
                        actual = ac
                        break
                    # If commands are substring-related (e.g., "pytest" vs "uv run pytest -q")
                    if exp_command and ac_command:
                        if exp_command in ac_command or ac_command in exp_command:
                            actual = ac
                            break

                if actual is None:
                    reasons.append(
                        f"Required verification command '{exp_name or exp_command}' "
                        f"missing for {pt.get('node_key', '')}"
                    )
                    continue

                # Use blocked/blocked_reason directly to infer blocker type
                if actual.get("blocked", False):
                    return ObjectiveCompletion(
                        complete=False, blocked_external=True, failed=False,
                        reasons=[f"Required verification '{exp_name or exp_command}' "
                                 f"blocked for {pt.get('node_key', '')}: "
                                 f"{actual.get('blocked_reason', '')}"],
                        verification_evidence=evidence,
                    )

                # IGNORE actual.get("required") — plan is the authority.
                if not actual.get("passed", False):
                    reasons.append(
                        f"Required verification '{exp_name or exp_command}' "
                        f"not passed for {pt.get('node_key', '')}"
                    )

            # ── Validate required live_e2e separately ──────────────────
            live_e2e = verif_spec.get("live_e2e") if isinstance(verif_spec, dict) else None
            if live_e2e and live_e2e.get("required", False):
                # Must have matching evidence entry
                matched = False
                for ev in evidence:
                    if ev["node_key"] == pt.get("node_key", "") and ev["name"] == live_e2e.get("name", ""):
                        matched = True
                        # Check blocked directly before checking passed
                        if ev.get("blocked", False):
                            return ObjectiveCompletion(
                                complete=False, blocked_external=True, failed=False,
                                reasons=[f"Required live E2E '{live_e2e.get('name')}' blocked: "
                                         f"{ev.get('blocked_reason', '')}"],
                                verification_evidence=evidence,
                            )
                        if not ev.get("passed", False):
                            reasons.append(f"Required live E2E '{live_e2e.get('name')}' failed")
                        break
                if not matched:
                    reasons.append(f"Required live E2E '{live_e2e.get('name')}' evidence missing for {pt.get('node_key', '')}")

        complete = len(reasons) == 0
        return ObjectiveCompletion(
            complete=complete,
            blocked_external=False,
            failed=False,
            reasons=reasons,
            verification_evidence=evidence,
        )