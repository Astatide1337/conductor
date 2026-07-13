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
        """Reconcile all active Composer objectives on each tick."""
        active_objectives = self.service.list_objectives(limit=100)
        for obj in active_objectives:
            composer_status = obj.get("composer_status", "")
            if composer_status in ("received", "normalizing", "normalized", "planning", "planned",
                                   "executing", "integrating", "verifying"):
                try:
                    if composer_status in ("received", "normalized", "planning", "planned"):
                        # Start/advance the pipeline for new objectives
                        await self.service.start_objective(obj["id"])
                    if composer_status in ("executing", "integrating", "verifying"):
                        await self.service.reconcile_objective(obj["id"])
                    # Also reconcile planned objectives to pick up task statuses
                    if composer_status == "planned":
                        await self.service.reconcile_objective(obj["id"])
                except Exception as exc:
                    logger.error("Reconcile failed for %s: %s", obj["id"], exc)
