"""MCP cockpit tools — expose conductor operations as MCP tools for any MCP-capable client."""

import json

from conductor.models import (
    ObjectiveCreate,
    TaskCreate,
    DryRunResult,
    GetStatusResult,
)


def _json(obj) -> str:
    return json.dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj


def register_conductor_tools(mcp, cfg, storage, breakers, skills_client, gateway_client):
    """Register all conductor MCP tools on a FastMCP server instance."""

    @mcp.tool()
    async def conductor_create_objective(
        title: str,
        description: str = "",
        priority: str = "normal",
        metadata_json: str = "{}",
    ) -> str:
        """Create a new objective and its initial run."""
        metadata = json.loads(metadata_json) if metadata_json else {}
        obj = storage.create_objective(title=title, description=description, priority=priority, metadata=metadata)
        run = storage.create_run(obj["id"], planner_mode=cfg.planner.mode)
        # Emit the objective.created event so cockpit audit views show what the HTTP API shows
        from conductor.events import emit
        emit(storage, "objective.created", f"Objective '{title}' created",
             objective_id=obj["id"], run_id=run["id"], source="mcp-user")
        return _json({"objective_id": obj["id"], "run_id": run["id"], "status": obj["status"]})

    @mcp.tool()
    async def conductor_get_objective(objective_id: str) -> str:
        """Get full objective details including runs."""
        obj = storage.get_objective(objective_id)
        if not obj:
            return _json({"error": "objective not found"})
        runs = storage.list_runs(objective_id, limit=5)
        return _json({"objective": obj, "runs": runs})

    @mcp.tool()
    async def conductor_list_objectives(status: str = "", limit: int = 50) -> str:
        """List all objectives, optionally filtered by status."""
        status_val = status if status else None
        objs = storage.list_objectives(status=status_val, limit=limit)
        return _json({"objectives": [{"id": o["id"], "title": o["title"], "status": o["status"], "priority": o["priority"]} for o in objs], "count": len(objs)})

    @mcp.tool()
    async def conductor_get_status(objective_id: str = "", run_id: str = "") -> str:
        """Get comprehensive status of objective, run, tasks, agent_runs, approvals, and events."""
        obj_id = objective_id or None
        r_id = run_id or None

        if not obj_id:
            objectives = storage.list_objectives(status="active", limit=1)
            if objectives:
                obj_id = objectives[0]["id"]

        objective = storage.get_objective(obj_id) if obj_id else None
        runs = storage.list_runs(obj_id, limit=1) if obj_id else []
        active_run = runs[0] if runs else None
        r_id = r_id or (active_run["id"] if active_run else None)

        tasks = storage.list_tasks(run_id=r_id, limit=50) if r_id else []
        approvals = storage.list_approvals(objective_id=obj_id, status="pending") if obj_id else []
        events = []  # lazy load if needed

        # Build circuit breaker status
        circuit_status = {}
        if r_id and active_run:
            from conductor.circuit import evaluate_cost_breaker, evaluate_iteration_breaker, evaluate_concurrency_breaker
            cost = evaluate_cost_breaker(storage, r_id, active_run.get("max_cost_usd", 10.0))
            iter_ = evaluate_iteration_breaker(storage, r_id, active_run.get("max_iterations", 50))
            conc = evaluate_concurrency_breaker(storage, r_id, active_run.get("max_concurrent_tasks", 4))
            circuit_status = {
                "cost": {"tripped": cost.tripped, "current": cost.current_value, "limit": cost.limit_value},
                "iterations": {"tripped": iter_.tripped, "current": iter_.current_value, "limit": iter_.limit_value},
                "concurrency": {"tripped": conc.tripped, "current": conc.current_value, "limit": conc.limit_value},
            }

        result = GetStatusResult(
            objective=objective or {},
            run=active_run or {},
            tasks=tasks,
            agent_runs=[],
            pending_approvals=approvals,
            recent_events=[dict(e) for e in events] if events else [],
            circuit_breakers=circuit_status,
        )
        return _json(result.model_dump())

    @mcp.tool()
    async def conductor_create_task(
        objective_id: str,
        title: str,
        brief: str = "",
        task_type: str = "ship",
        required_skills_json: str = "[]",
        depends_on_json: str = "[]",
        approval_required: bool = False,
    ) -> str:
        """Create a task under an objective's active run."""
        required_skills = json.loads(required_skills_json) if required_skills_json else []
        depends_on = json.loads(depends_on_json) if depends_on_json else []
        runs = storage.list_runs(objective_id, limit=1)
        if not runs:
            return _json({"error": "No active run for objective"})
        run_id = runs[-1]["id"]
        task = storage.create_task(
            objective_id, run_id, title, brief=brief,
            task_type=task_type, depends_on=depends_on,
            required_skills=required_skills, approval_required=approval_required,
        )
        return _json(task)

    @mcp.tool()
    async def conductor_dispatch_task(task_id: str) -> str:
        """Dispatch a task to Agents Gateway."""
        from conductor.dispatch import dispatch_task as do_dispatch
        try:
            result = do_dispatch(storage, gateway_client, task_id, skills_client=skills_client)
            return _json({"agent_run": result, "status": result["status"]})
        except Exception as e:
            return _json({"error": str(e), "task_id": task_id})

    @mcp.tool()
    async def conductor_list_approvals(
        objective_id: str = "",
        status: str = "pending",
    ) -> str:
        """List approval items, optionally filtered."""
        obj_id = objective_id if objective_id else None
        approvals = storage.list_approvals(objective_id=obj_id, status=status)
        return _json({"approvals": approvals, "count": len(approvals)})

    @mcp.tool()
    async def conductor_approve(approval_id: str, reason: str = "") -> str:
        """Approve a pending approval item."""
        updated = storage.update_approval_status(approval_id, "approved", decided_by="mcp-user", decision_reason=reason)
        if not updated:
            return _json({"error": "Approval not found"})
        return _json({"approval": updated, "status": "approved"})

    @mcp.tool()
    async def conductor_reject(approval_id: str, reason: str = "") -> str:
        """Reject a pending approval item."""
        updated = storage.update_approval_status(approval_id, "rejected", decided_by="mcp-user", decision_reason=reason)
        if not updated:
            return _json({"error": "Approval not found"})
        return _json({"approval": updated, "status": "rejected"})

    @mcp.tool()
    async def conductor_steer_objective(objective_id: str, guidance: str = "") -> str:
        """Add steering guidance to an objective (stored in metadata)."""
        obj = storage.get_objective(objective_id)
        if not obj:
            return _json({"error": "Objective not found"})
        meta = obj.get("metadata", {})
        meta["steering"] = guidance
        # Update via raw connection
        with storage.connect() as conn:
            conn.execute(
                "UPDATE objectives SET metadata_json = ? WHERE id = ?",
                (json.dumps(meta), objective_id),
            )
            conn.commit()
        return _json({"objective_id": objective_id, "steering_set": True})

    @mcp.tool()
    async def conductor_pause_objective(objective_id: str) -> str:
        """Pause an active objective."""
        obj = storage.get_objective(objective_id)
        if not obj:
            return _json({"error": "Objective not found"})
        if obj["status"] == "created":
            storage.update_objective_status(objective_id, "active")
        updated = storage.update_objective_status(objective_id, "paused")
        return _json({"objective": updated})

    @mcp.tool()
    async def conductor_resume_objective(objective_id: str) -> str:
        """Resume a paused or created objective."""
        obj = storage.get_objective(objective_id)
        if not obj:
            return _json({"error": "Objective not found"})
        if obj["status"] == "paused" or obj["status"] == "created":
            storage.update_objective_status(objective_id, "active")
        return _json({"objective": storage.get_objective(objective_id)})

    @mcp.tool()
    async def conductor_cancel_objective(objective_id: str) -> str:
        """Cancel an objective."""
        obj = storage.get_objective(objective_id)
        if not obj:
            return _json({"error": "Objective not found"})
        if obj["status"] == "created":
            storage.update_objective_status(objective_id, "active")
            obj = storage.get_objective(objective_id)
        if obj["status"] == "active":
            storage.update_objective_status(objective_id, "cancelled")
        return _json({"objective": storage.get_objective(objective_id)})

    @mcp.tool()
    async def conductor_dry_run(objective_id: str = "", run_id: str = "") -> str:
        """Run a deterministic dry-run and report what would happen."""
        from conductor.planner.deterministic import run_dry_run
        from conductor.circuit import BreakerEvaluator

        if not run_id:
            if not objective_id:
                objectives = storage.list_objectives(status="active", limit=1)
                if objectives:
                    objective_id = objectives[0]["id"]
            runs = storage.list_runs(objective_id, limit=1)
            if runs:
                run_id = runs[0]["id"]

        if not run_id:
            return _json({"error": "No active run found"})

        result = run_dry_run(storage, run_id, breakers, skills_client=skills_client)
        return _json(result.model_dump())

    @mcp.tool()
    async def conductor_reconcile() -> str:
        """Reconcile Conductor's view of in-flight agent_runs with the Agents Gateway.

        Safe to call anytime. Used after Conductor restart to recover durable state.
        Returns a summary: {reconciled, transitions, errors, candidate_count}.
        """
        from conductor.dispatch import reconcile_all
        summary = reconcile_all(storage, gateway_client)
        return _json(summary)

    @mcp.tool()
    async def conductor_view_events(
        objective_id: str = "",
        run_id: str = "",
        task_id: str = "",
        limit: int = 25,
    ) -> str:
        """View append-only audit events for an objective/run/task."""
        from conductor.events import list_events
        obj_id = objective_id or None
        r_id = run_id or None
        t_id = task_id or None
        events = list_events(storage, objective_id=obj_id, run_id=r_id, task_id=t_id, limit=limit)
        return _json({"events": [e.model_dump() for e in events], "count": len(events)})

    @mcp.tool()
    async def conductor_health_check() -> str:
        """Check conductor health and readiness."""
        return _json({
            "service": "astatide-conductor",
            "status": "healthy",
            "planner_mode": cfg.planner.mode,
            "auth_mode": cfg.auth.mode,
        })