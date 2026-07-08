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
from conductor.logging import (
    bind_request_context,
    clear_request_context,
    get_logger,
    setup_logging,
)
from conductor.metrics import MetricsRegistry, get_metrics_registry, init_conductor_metrics
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

    # ── Placeholder protected routes ───────────────────────────────────

    @app.post("/objectives")
    async def create_objective():
        raise HTTPException(501, "not implemented")

    @app.get("/objectives")
    async def list_objectives():
        raise HTTPException(501, "not implemented")

    @app.get("/objectives/{objective_id}")
    async def get_objective():
        raise HTTPException(501, "not implemented")

    return app


def run_server(cfg: ConductorConfig) -> None:
    setup_logging(cfg.observability.log_level, cfg.observability.log_format)
    logger.info("boot env=%s auth=%s port=%s", cfg.environment, cfg.auth.mode, cfg.service.port)

    app = create_app(cfg)

    cfg.auth.mode = cfg.auth.mode  # suppress unused warning — auth is wired in create_app

    uvicorn.run(app, host=cfg.service.host, port=cfg.service.port, log_level=cfg.observability.log_level.lower())