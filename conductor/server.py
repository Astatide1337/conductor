"""Conductor server — FastAPI + FastMCP hybrid, matching agent-gateway style.

Creates the FastAPI app, registers routes, auth middleware, and serves.
"""

import uuid
from contextvars import ContextVar

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from conductor import VERSION
from conductor.auth import AuthHandler, _make_auth_middleware_cls
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
from conductor.storage import ConductorStorage

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

    # Middleware
    AuthMw = _make_auth_middleware_cls(auth_handler)
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
        if obj["status"] not in ("active", "paused"):
            raise HTTPException(400, f"Cannot resume objective in '{obj['status']}' status")
        updated = storage.update_objective_status(objective_id, "active")
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

    @app.post("/tasks/{task_id}/dispatch")
    async def dispatch_task(task_id: str, request: Request):
        raise HTTPException(501, "dispatch not implemented — milestone 5")

    # ── Protected routes: Events ────────────────────────────────────────

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

    return app


def run_server(cfg: ConductorConfig) -> None:
    setup_logging(cfg.observability.log_level, cfg.observability.log_format)
    logger.info("boot env=%s auth=%s port=%s", cfg.environment, cfg.auth.mode, cfg.service.port)

    app = create_app(cfg)

    cfg.auth.mode = cfg.auth.mode  # suppress unused warning — auth is wired in create_app

    uvicorn.run(app, host=cfg.service.host, port=cfg.service.port, log_level=cfg.observability.log_level.lower())