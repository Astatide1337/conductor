"""Composer supervision loop — background reconciliation of Composer objectives.

Event-driven with periodic tick.  Pure sync design — launched as an
asyncio task from Conductor's ASGI lifespan.
"""

from __future__ import annotations

import asyncio
import logging

from conductor.composer.service import ComposerService

logger = logging.getLogger(__name__)

__all__ = ["ComposerSupervisor"]


class ComposerSupervisor:
    """Background supervision loop for Composer.

    Periodically reconciles active objectives.  Calls the LLM only when
    justified by a state transition (normalizing, planning, answering an
    interaction, restarting a failed agent, or generating final summary).
    """

    def __init__(
        self,
        service: ComposerService,
        *,
        poll_interval: float = 10.0,
        enabled: bool = True,
    ) -> None:
        self.service = service
        self.poll_interval = poll_interval
        self.enabled = enabled
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background loop."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="composer-supervisor")

    async def stop(self) -> None:
        """Stop the background loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=20.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                pass
            self._task = None

    async def _run(self) -> None:
        """Background loop — poll active objectives periodically."""
        logger.info("composer_supervisor_started interval=%s", self.poll_interval)
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("composer_supervisor_error: %s", exc)
            await asyncio.sleep(self.poll_interval)
        logger.info("composer_supervisor_stopped")

    async def _tick(self) -> None:
        """Reconcile all active Composer objectives on each tick.

        After a Conductor restart, persisted transitional states must advance
        idempotently.  Each transitional state gets explicit recovery:

        - normalizing  → safely rerun normalization
        - normalized   → proceed to planning
        - planning     → safely rerun planning (idempotent)
        - planned      → dispatch ready tasks
        - dispatching  → reconcile using idempotency key
        - executing    → reconcile tasks
        - integrating  → reconcile integration
        - verifying    → refetch verification evidence

        Objectives with composer_auto_start=False remain pending until
        explicitly started via POST /composer/objectives/{id}/start.

        Pause/resume preserves previous_status and paused_at so the exact
        prior state is restored — no second plan, no normalization rerun.
        """
        active_objectives = self.service.list_objectives(limit=100)
        for obj in active_objectives:
            composer_status = obj.get("composer_status", "")
            if composer_status in ("received", "normalizing", "normalized", "planning", "planned",
                                   "dispatching", "executing", "integrating", "verifying"):

                # auto_start=false objects must remain at 'received' until explicit start
                meta = obj.get("metadata", {}) or {}
                if composer_status == "received" and not meta.get("composer_auto_start", True):
                    continue

                try:
                    if composer_status in ("received", "normalizing", "normalized", "planning", "planned"):
                        await self.service.start_objective(obj["id"])
                    if composer_status in ("dispatching", "executing", "integrating", "verifying"):
                        await self.service.reconcile_objective(obj["id"])
                    if composer_status == "planned":
                        await self.service.reconcile_objective(obj["id"])
                except Exception as exc:
                    logger.error("Reconcile failed for %s: %s", obj["id"], exc)
