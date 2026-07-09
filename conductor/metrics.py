"""Prometheus-style metrics for Conductor."""

from dataclasses import dataclass, field
from threading import Lock
from typing import ClassVar


@dataclass
class MetricFamily:
    name: str
    type: str  # counter | gauge | histogram
    help: str
    labels: tuple[str, ...]
    values: dict[tuple[str, ...], float] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False)


class MetricsRegistry:
    def __init__(self) -> None:
        self._families: dict[str, MetricFamily] = {}
        self._lock = Lock()

    def register(self, name: str, type: str, help: str, labels: tuple[str, ...] = ()) -> MetricFamily:
        with self._lock:
            if name in self._families:
                return self._families[name]
            family = MetricFamily(name=name, type=type, help=help, labels=labels)
            self._families[name] = family
            return family

    def inc(self, name: str, amount: float = 1.0, labels: tuple[str, ...] = ()) -> None:
        family = self._families.get(name)
        if family is None:
            return
        with family._lock:
            family.values[labels] = family.values.get(labels, 0.0) + amount

    def set(self, name: str, value: float, labels: tuple[str, ...] = ()) -> None:
        family = self._families.get(name)
        if family is None:
            return
        with family._lock:
            family.values[labels] = value

    def observe(self, name: str, value: float, labels: tuple[str, ...] = ()) -> None:
        family = self._families.get(name)
        if family is None:
            return
        with family._lock:
            current = family.values.get(labels, 0.0)
            family.values[labels] = current + value

    def prometheus_text(self) -> str:
        lines: list[str] = []
        for family in sorted(self._families.values(), key=lambda f: f.name):
            lines.append(f"# HELP {family.name} {family.help}")
            lines.append(f"# TYPE {family.name} {family.type}")
            for label_tuple, value in sorted(family.values.items()):
                if label_tuple:
                    label_pairs = ",".join(
                        f'{k}="{v}"' for k, v in zip(family.labels, label_tuple)
                    )
                    lines.append(f"{family.name}{{{label_pairs}}} {value}")
                else:
                    lines.append(f"{family.name} {value}")
        return "\n".join(lines) + "\n"


_metrics_registry: MetricsRegistry | None = None


def get_metrics_registry() -> MetricsRegistry:
    global _metrics_registry
    if _metrics_registry is None:
        _metrics_registry = MetricsRegistry()
    return _metrics_registry


def init_conductor_metrics() -> None:
    reg = get_metrics_registry()
    reg.register("conductor_objectives_total", "counter", "Total objectives created")
    reg.register("conductor_objectives_active", "gauge", "Currently active objectives")
    reg.register("conductor_tasks_total", "counter", "Total tasks created")
    reg.register("conductor_tasks_running", "gauge", "Tasks currently running")
    reg.register("conductor_tasks_failed", "counter", "Failed task count")
    reg.register("conductor_agent_runs_total", "counter", "Total agent runs created")
    reg.register("conductor_approvals_pending", "gauge", "Approvals awaiting decision")
    reg.register("conductor_planner_turns_total", "counter", "Total planner invocations")
    reg.register("conductor_planner_errors_total", "counter", "Planner errors")
    reg.register("conductor_dispatch_errors_total", "counter", "Dispatch errors")
    reg.register("conductor_reconciliation_errors_total", "counter", "Reconciliation errors")
    reg.register("conductor_artifacts_ingested_total", "counter", "Total artifacts ingested")
    reg.register("conductor_circuit_breaker_trips_total", "counter", "Circuit breaker trips", ("breaker",))
    # ── Gateway Hub ────────────────────────────────────────────────────────
    reg.register("conductor_gateways_total", "gauge", "Registered gateways")
    reg.register("conductor_gateways_healthy", "gauge", "Healthy gateways")
    reg.register("conductor_gateways_unhealthy", "gauge", "Unhealthy gateways")
    reg.register("conductor_gateway_health_checks_total", "counter", "Gateway health checks performed")
    reg.register("conductor_gateway_health_check_errors_total", "counter", "Gateway health checks that errored")
    reg.register("conductor_capability_validation_total", "counter", "Task capability validations performed")
    reg.register("conductor_capability_validation_failed_total", "counter", "Task capability validations that failed")
    reg.register("conductor_gateway_actions_total", "counter", "Gateway actions dispatched", ("gateway_kind",))