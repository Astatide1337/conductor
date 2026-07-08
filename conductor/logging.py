"""Structured logging matching agent-gateway pattern.

Contextvars for request-scoped logging.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
objective_id_var: ContextVar[str | None] = ContextVar("objective_id", default=None)
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
task_id_var: ContextVar[str | None] = ContextVar("task_id", default=None)
auth_subject_var: ContextVar[str | None] = ContextVar("auth_subject", default=None)

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "cf-access-jwt-assertion",
    "x-auth-internal-token",
    "x-conductor-internal-token",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "lvl": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        for var, name in [
            (request_id_var, "req"),
            (objective_id_var, "obj"),
            (run_id_var, "run"),
            (task_id_var, "task"),
            (auth_subject_var, "sub"),
        ]:
            val = var.get()
            if val is not None:
                payload[name] = val
        if record.exc_info and record.exc_info[1] is not None:
            payload["err"] = str(record.exc_info[1])
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        parts = [datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), record.levelname, record.getMessage()]
        extra_info = []
        req = request_id_var.get()
        obj = objective_id_var.get()
        if req:
            extra_info.append(f"req={req}")
        if obj:
            extra_info.append(f"obj={obj}")
        suffix = f"[{' '.join(extra_info)}]" if extra_info else ""
        return f"{parts[0]} {parts[1]:>5} {parts[2]} {suffix}"


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger("conductor")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(root.level)

    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    elif fmt == "text":
        handler.setFormatter(TextFormatter())
    else:
        handler.setFormatter(TextFormatter())

    root.handlers = [handler]
    root.propagate = False


def bind_request_context(
    request_id: str,
    auth_subject: str | None = None,
    objective_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
) -> None:
    request_id_var.set(request_id)
    if auth_subject is not None:
        auth_subject_var.set(auth_subject)
    if objective_id is not None:
        objective_id_var.set(objective_id)
    if run_id is not None:
        run_id_var.set(run_id)
    if task_id is not None:
        task_id_var.set(task_id)


def clear_request_context() -> None:
    request_id_var.set(None)
    auth_subject_var.set(None)
    objective_id_var.set(None)
    run_id_var.set(None)
    task_id_var.set(None)


def get_logger() -> logging.Logger:
    return logging.getLogger("conductor")