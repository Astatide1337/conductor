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


def register_conductor_tools(mcp, cfg, storage, breakers, skills_client, gateway_client,
                             metrics=None, gateway_registry=None, mcp_gateway_client=None,
                             composer_service=None):
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
        required_capabilities_json: str = "[]",
        depends_on_json: str = "[]",
        approval_required: bool = False,
        metadata_json: str = "{}",
    ) -> str:
        """Create a task under an objective's active run.

        `required_capabilities_json` is a JSON list of dotted capability
        strings (e.g. ["execution.task.create","external.github"]) the caller
        requires Conductor's downstream gateway hub to provide. They are
        stored in task metadata `required_capabilities` and validated by
        the dispatch capability gate.
        """
        required_skills = json.loads(required_skills_json) if required_skills_json else []
        required_caps = json.loads(required_capabilities_json) if required_capabilities_json else []
        depends_on = json.loads(depends_on_json) if depends_on_json else []
        md = json.loads(metadata_json) if metadata_json else {}
        if required_caps:
            md["required_capabilities"] = required_caps
        runs = storage.list_runs(objective_id, limit=1)
        if not runs:
            return _json({"error": "No active run for objective"})
        run_id = runs[-1]["id"]
        task = storage.create_task(
            objective_id, run_id, title, brief=brief,
            task_type=task_type, depends_on=depends_on,
            required_skills=required_skills, approval_required=approval_required,
            metadata=md,
        )
        return _json(task)

    @mcp.tool()
    async def conductor_dispatch_task(task_id: str) -> str:
        """Dispatch a task to Agents Gateway."""
        from conductor.dispatch import dispatch_task as do_dispatch
        try:
            result = do_dispatch(
                storage, gateway_client, task_id,
                skills_client=skills_client, registry=gateway_registry, metrics=metrics,
            )
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
        summary = reconcile_all(storage, gateway_client, metrics=metrics)
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

    # ── Gateway Hub tools ────────────────────────────────────────────────
    # Conductor is the single hub; these tools let the cockpit see every
    # downstream gateway, its health, and its capabilities. No secrets are
    # exposed — GatewayConfig never carries tokens, and the HTTP probes only
    # return status/latency/version/capabilities.

    @mcp.tool()
    async def conductor_list_gateways() -> str:
        """List all configured downstream gateways (agents, skills, mcp, wiki, custom).

        Does not perform live health probes. Returns id, kind, name, enabled,
        configured (base_url present), auth_mode, timeout, and metadata.
        """
        if gateway_registry is None:
            return _json({"gateways": [], "count": 0})
        gateways = []
        for gw in gateway_registry.all():
            gateways.append({
                "id": gw.id, "kind": gw.kind, "name": gw.name,
                "enabled": gw.enabled, "configured": bool(gw.base_url),
                "auth_mode": gw.auth_mode,
                "timeout_seconds": gw.timeout_seconds,
                "metadata": gw.metadata,
            })
        return _json({"gateways": gateways, "count": len(gateways)})

    @mcp.tool()
    async def conductor_get_gateway_status(gateway_id: str) -> str:
        """Return the lightweight (non-probed) status of a single gateway.

        Possible statuses: unknown, not_configured, disabled.
        Use conductor_check_gateway_health for a live probe.
        """
        if gateway_registry is None:
            return _json({"error": "gateway registry disabled"})
        gw = gateway_registry.get(gateway_id)
        if not gw:
            return _json({"error": "gateway not found", "gateway_id": gateway_id})
        if not gw.base_url:
            st = "not_configured"
        elif not gw.enabled:
            st = "disabled"
        else:
            st = "unknown"
        return _json({
            "id": gw.id, "kind": gw.kind, "name": gw.name,
            "enabled": gw.enabled, "configured": bool(gw.base_url),
            "auth_mode": gw.auth_mode, "status": st,
            "healthy": st == "healthy",
        })

    @mcp.tool()
    async def conductor_check_gateway_health(gateway_id: str) -> str:
        """Live-probe a single downstream gateway and return its status.

        Returns status, latency_ms, version (if known), and capabilities.
        Emits a `gateway.health_checked` (or `gateway.health_failed`) event.
        """
        if gateway_registry is None:
            return _json({"error": "gateway registry disabled"})
        from conductor.gateways.health import check_gateway_health
        from conductor.gateways.events import emit_gateway_health_checked
        status = check_gateway_health(gateway_registry, gateway_id)
        if status is None:
            return _json({"error": "gateway not found", "gateway_id": gateway_id})
        try:
            emit_gateway_health_checked(
                storage, status.id, status.kind,
                status=status.status, latency_ms=status.latency_ms,
                capabilities=status.capabilities,
            )
        except Exception:
            pass
        if metrics:
            metrics.inc("conductor_gateway_health_checks_total")
            if status.status not in ("healthy", "not_configured", "disabled"):
                metrics.inc("conductor_gateway_health_check_errors_total")
        return _json(status.model_dump())

    @mcp.tool()
    async def conductor_check_all_gateways() -> str:
        """Live-probe every configured downstream gateway and return statuses."""
        if gateway_registry is None:
            return _json({"statuses": [], "count": 0})
        from conductor.gateways.health import check_all_gateways
        from conductor.gateways.events import emit_gateway_health_checked
        statuses = check_all_gateways(gateway_registry)
        for st in statuses:
            try:
                emit_gateway_health_checked(
                    storage, st.id, st.kind,
                    status=st.status, latency_ms=st.latency_ms,
                    capabilities=st.capabilities,
                )
            except Exception:
                pass
            if metrics:
                metrics.inc("conductor_gateway_health_checks_total")
                if st.status not in ("healthy", "not_configured", "disabled"):
                    metrics.inc("conductor_gateway_health_check_errors_total")
        return _json({
            "statuses": [s.model_dump() for s in statuses],
            "count": len(statuses),
        })

    @mcp.tool()
    async def conductor_list_capabilities(gateway_id: str = "") -> str:
        """List all capabilities known across the gateway hub, optionally filtered."""
        if gateway_registry is None:
            return _json({"capabilities": [], "count": 0})
        from conductor.gateways.capabilities import list_capabilities
        gid = gateway_id or None
        caps = list_capabilities(gateway_registry, gateway_id=gid)
        return _json({"capabilities": [c.model_dump() for c in caps], "count": len(caps)})

    @mcp.tool()
    async def conductor_find_capability(capability: str) -> str:
        """Find candidate gateways that can provide a capability.

        Useful for "Where can I get external.github?" type questions from a
        cockpit. Returns the list of (gateway_id, gateway_kind, available)
        entries — operator may then dispatch to whichever makes sense.
        """
        if gateway_registry is None:
            return _json({"capability": capability, "candidates": [], "count": 0})
        from conductor.gateways.capabilities import find_gateways_for_capability
        candidates = find_gateways_for_capability(gateway_registry, capability)
        return _json({
            "capability": capability,
            "candidates": [c.model_dump() for c in candidates],
            "count": len(candidates),
        })

    @mcp.tool()
    async def conductor_call_mcp_gateway_tool(
        tool_name: str,
        arguments_json: str = "{}",
        objective_id: str = "",
    ) -> str:
        """EXPERIMENTAL — policy-gated invocation of a downstream MCP Gateway tool.

        Only intended for cockpit operators who explicitly want to drive the
        MCP Gateway through Conductor. Conductor emits a
        `gateway.mcp.tool_call` event in the timeline. Tokens and arguments
        are never logged verbatim — only the tool name is recorded.

        Returns the raw tool result body, or an error object if the MCP
        Gateway is not configured or the tool call fails.
        """
        if mcp_gateway_client is None:
            return _json({"error": "MCP Gateway client is not configured"})
        args = json.loads(arguments_json) if arguments_json else {}
        obj_id = objective_id or None
        from conductor.gateways.events import emit_gateway_mcp_tool_call
        try:
            result = mcp_gateway_client.call_tool(tool_name, args)
            try:
                emit_gateway_mcp_tool_call(
                    storage, gateway_id="mcp", tool_name=tool_name,
                    arguments=None, objective_id=obj_id,
                )
            except Exception:
                pass
            if metrics:
                metrics.inc("conductor_gateway_actions_total", labels=("mcp",))
            return _json({"tool": tool_name, "result": result, "ok": True})
        except Exception as e:
            return _json({"tool": tool_name, "error": str(e), "ok": False})

    @mcp.tool()
    async def conductor_get_timeline(objective_id: str, limit: int = 200) -> str:
        """Return the chronological timeline of all events for an objective.

        Includes objective/task lifecycle, gateway checks, dispatches,
        approvals, and reconciliations — everything Conductor knows about
        what happened with a given objective. Cockpits should use this as
        the "What happened with objective X?" entry point.
        """
        from conductor.events import list_events
        events = list_events(storage, objective_id=objective_id, limit=limit)
        chronological = [e.model_dump() for e in reversed(events)]
        return _json({"objective_id": objective_id, "count": len(chronological),
                      "events": chronological})

    # ── Composer MCP tools ────────────────────────────────────────────────

    if composer_service is not None:

        @mcp.tool()
        async def composer_submit_spec(
            title: str,
            spec: str,
            repository_json: str = "{}",
            auto_start: bool = True,
        ) -> str:
            """Submit a finalized specification to Composer for autonomous execution."""
            repo = json.loads(repository_json) if repository_json else {}
            result = await composer_service.submit_specification(
                title=title, raw_spec=spec, repository=repo, auto_start=auto_start,
            )
            return _json(result)

        @mcp.tool()
        async def composer_list_objectives(status: str = "", limit: int = 50, offset: int = 0) -> str:
            """List Composer objectives."""
            objs = composer_service.list_objectives(status=status or None, limit=limit, offset=offset)
            return _json({"objectives": objs})

        @mcp.tool()
        async def composer_get_objective(objective_id: str) -> str:
            """Get full Composer objective state including spec and plan."""
            obj = composer_service.get_objective(objective_id)
            return _json(obj or {"error": "not found"})

        @mcp.tool()
        async def composer_get_plan(objective_id: str) -> str:
            """Get the Composer plan for an objective."""
            plan = composer_service.get_plan(objective_id)
            return _json(plan or {"error": "not found"})

        @mcp.tool()
        async def composer_get_status(objective_id: str) -> str:
            """Get concise Composer objective status with progress summary."""
            obj = composer_service.get_objective(objective_id) or {}
            spec = obj.get("composer_spec", {})
            plan = obj.get("composer_plan", {})
            tasks = plan.get("plan_tasks", [])
            completed = sum(1 for t in tasks if t.get("status") == "completed")
            running = sum(1 for t in tasks if t.get("status") in ("running", "dispatching"))
            pending = sum(1 for t in tasks if t.get("status") == "pending")
            return _json({
                "objective_id": objective_id,
                "status": spec.get("status", "unknown"),
                "progress": {
                    "total_tasks": len(tasks),
                    "completed": completed,
                    "running": running,
                    "pending": pending,
                },
                "blocked_external": spec.get("status") == "blocked_external",
                "report_available": bool(composer_service.get_report(objective_id)),
            })

        @mcp.tool()
        async def composer_get_timeline(objective_id: str, limit: int = 100) -> str:
            """Get the Composer event timeline for an objective."""
            events = composer_service.get_timeline(objective_id)
            return _json({"objective_id": objective_id, "events": events})

        @mcp.tool()
        async def composer_get_report(objective_id: str) -> str:
            """Get the Composer review report for an objective."""
            report = composer_service.get_report(objective_id)
            return _json(report or {"error": "not found"})

        @mcp.tool()
        async def composer_pause(objective_id: str) -> str:
            """Pause a Composer objective."""
            result = await composer_service.pause_objective(objective_id)
            return _json(result or {"error": "not found"})

        @mcp.tool()
        async def composer_resume(objective_id: str) -> str:
            """Resume a Composer objective."""
            result = await composer_service.resume_objective(objective_id)
            return _json(result)

        @mcp.tool()
        async def composer_cancel(objective_id: str) -> str:
            """Cancel a Composer objective."""
            result = await composer_service.cancel_objective(objective_id)
            return _json(result or {"error": "not found"})

        @mcp.tool()
        async def composer_reconcile(objective_id: str) -> str:
            """Trigger Composer reconciliation for an objective."""
            result = await composer_service.reconcile_objective(objective_id)
            return _json(result)

        @mcp.tool()
        async def composer_steer(objective_id: str, guidance: str) -> str:
            """Add steering guidance to an active Composer objective."""
            result = await composer_service.steer_objective(objective_id, guidance)
            return _json(result)