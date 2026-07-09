"""Conductor server — FastAPI + FastMCP hybrid, matching agent-gateway style.

Creates the FastAPI app, registers routes, auth middleware, and serves.
"""

import uuid
from contextvars import ContextVar

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from conductor import VERSION
from conductor.auth import AuthHandler, _make_auth_middleware_cls, make_mcp_auth_middleware_cls
from conductor.config import ConductorConfig
from conductor.events import emit, list_events as list_events_fn
from conductor.logging import (
    bind_request_context,
    clear_request_context,
    get_logger,
    setup_logging,
)
from conductor.metrics import MetricsRegistry, get_metrics_registry, init_conductor_metrics
from conductor.models import ObjectiveCreate, TaskCreate
from conductor.policy import check_action, check_decision
from conductor.storage import ConductorStorage
from conductor.clients.agents_gateway import (
    BaseAgentsGatewayClient,
    HttpAgentsGatewayClient,
    MockAgentsGatewayClient,
)
from conductor.clients.skills_gateway import (
    BaseSkillsGatewayClient,
    HttpSkillsGatewayClient,
    MockSkillsGatewayClient,
)
from conductor.clients.mcp_gateway import (
    BaseMcpGatewayClient,
    HttpMcpGatewayClient,
    MockMcpGatewayClient,
    McpGatewayError,
)
from conductor.gateways import build_default_registry
from conductor.gateways.registry import GatewayRegistry, GatewayConfig


def _build_gateway_client(cfg: ConductorConfig) -> BaseAgentsGatewayClient:
    """Build the Agents Gateway client. Real HTTP when CONDUCTOR_AGENTS_GATEWAY_URL
    is set to a non-localhost URL, otherwise a Mock client for offline dev/test.
    Provides a shared configured agents gateway actor to the HTTP routes + MCP tools,
    so dispatch and reconcile use the same gateway instance."""
    gw_cfg = cfg.agents_gateway
    if gw_cfg.url and gw_cfg.auth_mode != "dev-none" and not _is_localhost_url(gw_cfg.url):
        return HttpAgentsGatewayClient(gw_cfg)
    # Mock for offline / dev
    mock = MockAgentsGatewayClient()
    mock.register_agent("code-validator", "Code Validator")
    return mock


def _build_skills_client(cfg: ConductorConfig) -> BaseSkillsGatewayClient | None:
    sk_cfg = cfg.skills_gateway
    if sk_cfg.url and sk_cfg.auth_mode != "dev-none" and not _is_localhost_url(sk_cfg.url):
        return HttpSkillsGatewayClient(sk_cfg)
    # None means "skills validation is a no-op" — falls back to "all skills valid"
    # In dev / offline we register nothing, so the dry-run will note missing skills.
    return None


def _build_mcp_gateway_client(cfg: ConductorConfig) -> BaseMcpGatewayClient | None:
    """Real MCP Gateway client when URL + non-dev auth configured; None otherwise.

    Conductor never presents dev-none to a downstream gateway if the operator
    told us the MCP Gateway URL is real — we drop back to None so the registry
    can mark the gateway not_configured for the operator to wire up.
    """
    mcp_cfg = cfg.mcp_gateway
    if mcp_cfg.url and mcp_cfg.auth_mode != "dev-none" and not _is_localhost_url(mcp_cfg.url):
        try:
            return HttpMcpGatewayClient(mcp_cfg)
        except McpGatewayError as e:
            logger.warning("mcp_gateway_init_failed url=%s err=%s", mcp_cfg.url, e)
            return None
    if mcp_cfg.url and _is_localhost_url(mcp_cfg.url):
        # Pure-local dev mock — easy offline experimentation.
        return MockMcpGatewayClient()
    return None


def _is_localhost_url(url: str) -> bool:
    return "://localhost" in url or "://127.0.0.1" in url or "://0.0.0.0" in url

try:
    from fastmcp import FastMCP
    HAS_FASTMCP = True
except ImportError:
    HAS_FASTMCP = False


def _build_mcp_app(cfg: ConductorConfig, storage: ConductorStorage,
                   gateway_client, skills_client, mcp_gateway_client,
                   gateway_registry, metrics_reg) -> tuple | None:
    """Build the FastMCP ASGI sub-app + its lifespan, ready to mount.

    Returns `(mount_path, mcp_asgi)` if FastMCP is available, else None.

    The caller is responsible for:
      1. passing `mcp_asgi.lifespan` to the parent FastAPI app constructor so
         FastMCP's StreamableHTTPSessionManager can initialize its task group;
      2. calling `mcp_asgi.add_middleware(MCPAuthMw)` to share Conductor's
         auth model on the MCP surface;
      3. mounting it at `mount_path` on the parent app.

    Without step (1), FastMCP returns 500 on every JSON-RPC call with the
    "task group is not initialized" error — see
    https://gofastmcp.com/deployment/asgi.
    """
    if not HAS_FASTMCP:
        return None
    from conductor.mcp_tools import register_conductor_tools
    from conductor.circuit import BreakerEvaluator

    mcp_server = FastMCP(
        "Astatide Conductor",
        instructions="Persistent objective orchestrator for MCP-driven agent workflows.",
    )
    b_evaluator = BreakerEvaluator(
        storage,
        max_iterations=cfg.circuit.max_iterations_per_run,
        max_cost_usd=cfg.circuit.max_cost_usd_per_run,
        max_concurrent=cfg.circuit.max_concurrent_tasks,
        max_retries=cfg.circuit.max_retries_per_task,
        max_wall_clock=cfg.circuit.max_wall_clock_minutes,
        max_stall=cfg.circuit.max_stall_minutes,
    )
    register_conductor_tools(
        mcp_server, cfg, storage, b_evaluator,
        skills_client, gateway_client,
        metrics=metrics_reg,
        gateway_registry=gateway_registry,
        mcp_gateway_client=mcp_gateway_client,
    )
    mcp_asgi = mcp_server.http_app(path=cfg.service.mcp_path)
    return (cfg.service.mcp_path, mcp_asgi)

logger = get_logger()


def create_app(cfg: ConductorConfig, metrics_reg: MetricsRegistry | None = None) -> FastAPI:
    setup_logging(cfg.observability.log_level, cfg.observability.log_format)

    storage = ConductorStorage(cfg.storage.sqlite_path)
    storage.initialize()

    auth_handler = AuthHandler(cfg.auth)
    auth_handler.require_production_safe()

    if metrics_reg is None and cfg.observability.metrics_enabled:
        init_conductor_metrics()
        metrics_reg = get_metrics_registry()

    # Build MCP sub-app BEFORE the parent FastAPI app so we can propagate
    # FastMCP's lifespan to the parent — without this, FastMCP's streamable
    # HTTP session manager raises "task group is not initialized" on every
    # JSON-RPC call (see https://gofastmcp.com/deployment/asgi).
    # If FastMCP is unavailable, mcp_pair is None and the lifespan stays None.
    mcp_pair = _build_mcp_app(
        cfg, storage,
        _build_gateway_client(cfg), _build_skills_client(cfg),
        _build_mcp_gateway_client(cfg), build_default_registry(cfg),
        metrics_reg,
    )
    mcp_lifespan = mcp_pair[1].lifespan if mcp_pair else None

    app = FastAPI(
        title="Astatide Conductor",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=mcp_lifespan,
    )

    # Store shared state
    app.state.config = cfg
    app.state.storage = storage
    app.state.auth = auth_handler
    app.state.metrics = metrics_reg
    app.state.gateway_client = _build_gateway_client(cfg)
    app.state.skills_client = _build_skills_client(cfg)
    app.state.mcp_gateway_client = _build_mcp_gateway_client(cfg)
    app.state.gateway_registry = build_default_registry(cfg)

    # Emit gateway.registered events once at startup so the operator timeline
    # reflects the configured hub at boot.
    from conductor.gateways.events import emit_gateway_registered
    for gw in app.state.gateway_registry.all():
        try:
            emit_gateway_registered(storage, gw.id, gw.kind, gw.name)
        except Exception as e:
            logger.warning("gateway_registered_emit_failed id=%s err=%s", gw.id, e)

    # Middleware — outer middleware handles /mcp with JSON-RPC-shaped errors
    # (sub-app middleware below is defense-in-depth).
    AuthMw = _make_auth_middleware_cls(auth_handler, mcp_path=cfg.service.mcp_path)
    app.add_middleware(AuthMw)

    # ── Public routes ──────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "astatide-conductor"}

    @app.get("/ready")
    async def ready():
        checks: dict = {}
        try:
            storage.connect().close()
            checks["storage"] = "ok"
        except Exception as e:
            checks["storage"] = f"error: {e}"
        checks["auth_mode"] = cfg.auth.mode
        checks["planner_mode"] = cfg.planner.mode

        storage_ok = checks.get("storage") == "ok"
        return JSONResponse(
            {"ready": storage_ok, "checks": checks},
            status_code=200 if storage_ok else 503,
        )

    @app.get("/version")
    async def version():
        return {"service": "astatide-conductor", "version": VERSION, "environment": cfg.environment}

    @app.get("/metrics")
    async def metrics():
        if not cfg.observability.metrics_enabled or not metrics_reg:
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse("metrics disabled", status_code=404)
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(metrics_reg.prometheus_text(), media_type="text/plain; charset=utf-8")

    # ── Protected routes: Objectives ──────────────────────────────────────

    @app.post("/objectives")
    async def create_objective(body: ObjectiveCreate, request: Request):
        obj = storage.create_objective(
            title=body.title,
            description=body.description,
            priority=body.priority,
            created_by=body.created_by,
            metadata=body.metadata,
        )
        circuit = cfg.circuit
        run = storage.create_run(
            obj["id"],
            planner_mode=cfg.planner.mode,
            max_iterations=circuit.max_iterations_per_run,
            max_cost_usd=circuit.max_cost_usd_per_run,
            max_concurrent_tasks=circuit.max_concurrent_tasks,
        )
        emit(storage, "objective.created", f"Objective '{body.title}' created",
             objective_id=obj["id"], run_id=run["id"], source="user")
        if metrics_reg:
            metrics_reg.inc("conductor_objectives_total")
            metrics_reg.inc("conductor_objectives_active")
        return JSONResponse({"objective_id": obj["id"], "run_id": run["id"], "status": obj["status"]}, status_code=201)

    @app.get("/objectives")
    async def list_objectives(request: Request, status: str | None = None, limit: int = 50, offset: int = 0):
        objs = storage.list_objectives(status=status, limit=limit, offset=offset)
        return {"objectives": objs, "count": len(objs)}

    @app.get("/objectives/{objective_id}")
    async def get_objective(objective_id: str, request: Request):
        obj = storage.get_objective(objective_id)
        if not obj:
            raise HTTPException(404, "Objective not found")
        runs = storage.list_runs(objective_id, limit=1)
        return {"objective": obj, "runs": runs}

    @app.post("/objectives/{objective_id}/pause")
    async def pause_objective(objective_id: str, request: Request):
        obj = storage.get_objective(objective_id)
        if not obj:
            raise HTTPException(404, "Objective not found")
        if obj["status"] == "created":
            storage.update_objective_status(objective_id, "active")
        updated = storage.update_objective_status(objective_id, "paused")
        if not updated:
            raise HTTPException(400, "Cannot pause")
        emit(storage, "objective.paused", f"Objective paused", objective_id=objective_id, source="user")
        return {"objective": updated}

    @app.post("/objectives/{objective_id}/resume")
    async def resume_objective(objective_id: str, request: Request):
        obj = storage.get_objective(objective_id)
        if not obj:
            raise HTTPException(404, "Objective not found")
        if obj["status"] not in ("created", "active", "paused"):
            raise HTTPException(400, f"Cannot resume objective in '{obj['status']}' status")
        if obj["status"] == "paused":
            updated = storage.update_objective_status(objective_id, "active")
        elif obj["status"] == "created":
            updated = storage.update_objective_status(objective_id, "active")
        else:
            updated = obj  # already active
        emit(storage, "objective.resumed", f"Objective resumed", objective_id=objective_id, source="user")
        return {"objective": updated}

    @app.post("/objectives/{objective_id}/cancel")
    async def cancel_objective(objective_id: str, request: Request):
        obj = storage.get_objective(objective_id)
        if not obj:
            raise HTTPException(404, "Objective not found")
        # Can only cancel from active state (after created->active transition)
        if obj["status"] in ("created", "active"):
            # Must go through active first if created
            if obj["status"] == "created":
                storage.update_objective_status(objective_id, "active")
            storage.update_objective_status(objective_id, "cancelled")
        else:
            raise HTTPException(400, f"Cannot cancel objective in '{obj['status']}' status")
        emit(storage, "objective.cancelled", f"Objective cancelled", objective_id=objective_id, source="user")
        return {"objective": storage.get_objective(objective_id)}

    # ── Protected routes: Tasks ─────────────────────────────────────────

    @app.post("/objectives/{objective_id}/tasks")
    async def create_task(objective_id: str, body: TaskCreate, request: Request):
        obj = storage.get_objective(objective_id)
        if not obj:
            raise HTTPException(404, "Objective not found")
        runs = storage.list_runs(objective_id, limit=1)
        if not runs:
            raise HTTPException(400, "No active run for objective")
        run_id = runs[-1]["id"]
        task = storage.create_task(
            objective_id, run_id, body.title, brief=body.brief,
            task_type=body.task_type, depends_on=body.depends_on,
            required_skills=body.required_skills, dispatch_profile=body.dispatch_profile,
            approval_required=body.approval_required, metadata=body.metadata,
        )
        emit(storage, "task.created", f"Task '{body.title}' created",
             objective_id=objective_id, run_id=run_id, task_id=task["id"], source="user")
        if metrics_reg:
            metrics_reg.inc("conductor_tasks_total")
        return JSONResponse(task, status_code=201)

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        task = storage.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        agent_runs = []  # agent_runs not queryable by task_id yet
        return {"task": task, "agent_runs": agent_runs}

    @app.get("/tasks")
    async def list_tasks(
        objective_id: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        tasks = storage.list_tasks(objective_id=objective_id, run_id=run_id, status=status, limit=limit, offset=offset)
        return {"tasks": tasks, "count": len(tasks)}

    @app.post("/tasks/{task_id}/dispatch")
    async def dispatch_route(task_id: str, request: Request):
        from conductor.dispatch import dispatch_task as do_dispatch
        try:
            result = do_dispatch(
                app.state.storage,
                app.state.gateway_client,
                task_id,
                skills_client=app.state.skills_client,
                registry=app.state.gateway_registry,
                metrics=app.state.metrics,
            )
            return JSONResponse(result, status_code=200)
        except Exception as e:
            raise HTTPException(500, f"Dispatch failed: {e}")

    # ── Protected routes: Approvals ──────────────────────────────────────

    @app.get("/approvals")
    async def list_approvals(
        objective_id: str | None = None,
        run_id: str | None = None,
        status: str = "pending",
        limit: int = 50,
    ):
        approvals = storage.list_approvals(objective_id=objective_id, run_id=run_id, status=status, limit=limit)
        return {"approvals": approvals, "count": len(approvals)}

    @app.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str, request: Request):
        updated = storage.update_approval_status(approval_id, "approved", decided_by="user")
        if not updated:
            raise HTTPException(404, "Approval not found")
        emit(storage, "approval.approved", f"Approval {approval_id} approved",
             objective_id=updated["objective_id"], run_id=updated["run_id"], source="user")
        return {"approval": updated}

    @app.post("/approvals/{approval_id}/reject")
    async def reject(approval_id: str, request: Request):
        updated = storage.update_approval_status(approval_id, "rejected", decided_by="user")
        if not updated:
            raise HTTPException(404, "Approval not found")
        emit(storage, "approval.rejected", f"Approval {approval_id} rejected",
             objective_id=updated["objective_id"], run_id=updated["run_id"], source="user")
        return {"approval": updated}

    # ── Protected routes: Reconciliation ─────────────────────────────────

    @app.post("/reconcile")
    async def reconcile(request: Request):
        from conductor.dispatch import reconcile_all
        summary = reconcile_all(
            app.state.storage,
            app.state.gateway_client,
            metrics=app.state.metrics,
        )
        return JSONResponse(summary, status_code=200)

    # ── Protected routes: Dry run ────────────────────────────────────────

    @app.post("/dry-run")
    async def dry_run(request: Request):
        from conductor.planner.deterministic import run_dry_run
        from conductor.circuit import BreakerEvaluator
        objectives = storage.list_objectives(status="active", limit=1)
        if not objectives:
            return {"message": "No active objectives for dry run"}
        obj = objectives[0]
        runs = storage.list_runs(obj["id"], limit=1)
        if not runs:
            return {"message": "No active run for dry run"}
        run = runs[0]
        circuit = cfg.circuit
        evaluator = BreakerEvaluator(
            storage,
            max_iterations=circuit.max_iterations_per_run,
            max_cost_usd=circuit.max_cost_usd_per_run,
            max_concurrent=circuit.max_concurrent_tasks,
            max_retries=circuit.max_retries_per_task,
            max_wall_clock=circuit.max_wall_clock_minutes,
            max_stall=circuit.max_stall_minutes,
        )
        result = run_dry_run(storage, run["id"], evaluator)
        return result.model_dump()

    @app.get("/events")
    async def list_events(
        objective_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        from fastapi import Request
        events = list_events_fn(storage, objective_id=objective_id, run_id=run_id, task_id=task_id, limit=limit, offset=offset)
        events_serialized = [e.model_dump() for e in events]
        return {"events": events_serialized, "count": len(events_serialized)}

    # ── Protected routes: Gateway Hub ─────────────────────────────────────

    from conductor.gateways.capabilities import (
        list_capabilities as _list_caps,
        find_gateways_for_capability as _find_caps,
    )
    from conductor.gateways.health import (
        check_gateway_health as _check_health,
        check_all_gateways as _check_all,
    )
    from conductor.gateways.events import emit_gateway_health_checked

    def _serialize_gateway(gw: GatewayConfig) -> dict:
        return {
            "id": gw.id, "kind": gw.kind, "name": gw.name,
            "enabled": gw.enabled, "configured": bool(gw.base_url),
            "base_url_present": bool(gw.base_url),
            "auth_mode": gw.auth_mode,
            "health_path": gw.health_path, "version_path": gw.version_path,
            "timeout_seconds": gw.timeout_seconds,
            "metadata": gw.metadata,
        }

    @app.get("/gateways")
    async def list_gateways(request: Request):
        reg: GatewayRegistry = app.state.gateway_registry
        return {"gateways": [_serialize_gateway(g) for g in reg.all()],
                "count": len(reg.all())}

    @app.get("/gateways/status")
    async def gateways_status(request: Request):
        reg: GatewayRegistry = app.state.gateway_registry
        statuses = []
        for gw in reg.all():
            if not gw.base_url:
                st = "not_configured"
            elif not gw.enabled:
                st = "disabled"
            else:
                st = "unknown"
            statuses.append({
                "id": gw.id, "kind": gw.kind, "name": gw.name,
                "enabled": gw.enabled, "configured": bool(gw.base_url),
                "base_url_present": bool(gw.base_url),
                "auth_mode": gw.auth_mode,
                "status": st,
                "healthy": st == "healthy",
                "last_checked_at": "",
            })
        return {"gateways": statuses, "count": len(statuses)}

    @app.get("/gateways/{gateway_id}")
    async def get_gateway(gateway_id: str, request: Request):
        reg: GatewayRegistry = app.state.gateway_registry
        gw = reg.get(gateway_id)
        if not gw:
            raise HTTPException(404, "Gateway not found")
        return {"gateway": _serialize_gateway(gw)}

    @app.post("/gateways/{gateway_id}/check")
    async def check_gateway(gateway_id: str, request: Request):
        reg: GatewayRegistry = app.state.gateway_registry
        if not reg.has(gateway_id):
            raise HTTPException(404, "Gateway not found")
        status = _check_health(reg, gateway_id)
        if status is None:
            raise HTTPException(404, "Gateway not found")
        try:
            emit_gateway_health_checked(
                storage, status.id, status.kind,
                status=status.status,
                latency_ms=status.latency_ms,
                capabilities=status.capabilities,
            )
        except Exception as e:
            logger.warning("gateway_health_emit_failed id=%s err=%s", gateway_id, e)
        if metrics_reg:
            metrics_reg.inc("conductor_gateway_health_checks_total")
            if status.status not in ("healthy", "not_configured", "disabled"):
                metrics_reg.inc("conductor_gateway_health_check_errors_total")
        return {"status": status.model_dump()}

    @app.post("/gateways/check-all")
    async def check_all(request: Request):
        reg: GatewayRegistry = app.state.gateway_registry
        statuses = _check_all(reg)
        for st in statuses:
            try:
                emit_gateway_health_checked(
                    storage, st.id, st.kind,
                    status=st.status, latency_ms=st.latency_ms,
                    capabilities=st.capabilities,
                )
            except Exception as e:
                logger.warning("gateway_health_emit_failed id=%s err=%s", st.id, e)
            if metrics_reg:
                metrics_reg.inc("conductor_gateway_health_checks_total")
                if st.status not in ("healthy", "not_configured", "disabled"):
                    metrics_reg.inc("conductor_gateway_health_check_errors_total")
        if metrics_reg:
            healthy_count = sum(1 for s in statuses if s.status == "healthy")
            unhealthy_count = len(statuses) - healthy_count
            metrics_reg.set("conductor_gateways_total", float(len(reg.all())))
            metrics_reg.set("conductor_gateways_healthy", float(healthy_count))
            metrics_reg.set("conductor_gateways_unhealthy", float(unhealthy_count))
        return {"statuses": [s.model_dump() for s in statuses], "count": len(statuses)}

    @app.get("/capabilities")
    async def list_capabilities_route(request: Request, gateway_id: str | None = None):
        reg: GatewayRegistry = app.state.gateway_registry
        caps = _list_caps(reg, gateway_id=gateway_id)
        return {"capabilities": [c.model_dump() for c in caps], "count": len(caps)}

    @app.get("/capabilities/{capability}")
    async def get_capability(capability: str, request: Request):
        reg: GatewayRegistry = app.state.gateway_registry
        candidates = _find_caps(reg, capability)
        return {"capability": capability, "candidates": [c.model_dump() for c in candidates],
                "count": len(candidates)}

    # ── Protected routes: Objective timeline ────────────────────────────────

    @app.get("/objectives/{objective_id}/timeline")
    async def get_objective_timeline(objective_id: str, request: Request, limit: int = 200):
        obj = storage.get_objective(objective_id)
        if not obj:
            raise HTTPException(404, "Objective not found")
        events = list_events_fn(storage, objective_id=objective_id, limit=limit, offset=0)
        timeline = [e.model_dump() for e in reversed(events)]
        return {"objective_id": objective_id, "count": len(timeline), "events": timeline}

    # ── MCP server mount ────────────────────────────────────────────────
    # The MCP sub-app was built earlier in create_app so its lifespan could
    # be propagated to the parent FastAPI app (FastMCP requires this for
    # its StreamableHTTPSessionManager to initialize). Here we just attach
    # the auth middleware and mount it.

    if mcp_pair is not None:
        mount_path, mcp_asgi = mcp_pair
        MCPAuthMw = make_mcp_auth_middleware_cls(auth_handler)
        mcp_asgi.add_middleware(MCPAuthMw)
        app.mount(mount_path, mcp_asgi)

    return app


def run_server(cfg: ConductorConfig) -> None:
    setup_logging(cfg.observability.log_level, cfg.observability.log_format)
    logger.info("boot env=%s auth=%s port=%s", cfg.environment, cfg.auth.mode, cfg.service.port)

    app = create_app(cfg)

    cfg.auth.mode = cfg.auth.mode  # suppress unused warning — auth is wired in create_app

    uvicorn.run(app, host=cfg.service.host, port=cfg.service.port, log_level=cfg.observability.log_level.lower())