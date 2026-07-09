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


def _is_localhost_url(url: str) -> bool:
    return "://localhost" in url or "://127.0.0.1" in url or "://0.0.0.0" in url

try:
    from fastmcp import FastMCP
    HAS_FASTMCP = True
except ImportError:
    HAS_FASTMCP = False

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

    app = FastAPI(
        title="Astatide Conductor",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Store shared state
    app.state.config = cfg
    app.state.storage = storage
    app.state.auth = auth_handler
    app.state.metrics = metrics_reg
    app.state.gateway_client = _build_gateway_client(cfg)
    app.state.skills_client = _build_skills_client(cfg)

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

    # ── MCP server mount ────────────────────────────────────────────────

    if HAS_FASTMCP:
        from conductor.mcp_tools import register_conductor_tools
        from conductor.circuit import BreakerEvaluator

        mcp_server = FastMCP("Astatide Conductor", instructions="Persistent objective orchestrator for MCP-driven agent workflows.")

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
            app.state.skills_client, app.state.gateway_client,
            metrics=app.state.metrics,
        )

        mcp_asgi = mcp_server.http_app(path=cfg.service.mcp_path)
        # Wrap MCP sub-app in the same auth model as the rest of Conductor.
        # No public paths on /mcp — any cockpit must identify itself via internal token or CF JWT.
        MCPAuthMw = make_mcp_auth_middleware_cls(auth_handler)
        mcp_asgi.add_middleware(MCPAuthMw)
        app.mount(cfg.service.mcp_path, mcp_asgi)

    return app


def run_server(cfg: ConductorConfig) -> None:
    setup_logging(cfg.observability.log_level, cfg.observability.log_format)
    logger.info("boot env=%s auth=%s port=%s", cfg.environment, cfg.auth.mode, cfg.service.port)

    app = create_app(cfg)

    cfg.auth.mode = cfg.auth.mode  # suppress unused warning — auth is wired in create_app

    uvicorn.run(app, host=cfg.service.host, port=cfg.service.port, log_level=cfg.observability.log_level.lower())