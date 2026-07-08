"""Tests for Prometheus-style metrics."""


from conductor.metrics import (
    MetricsRegistry,
    init_conductor_metrics,
    get_metrics_registry,
)


class TestMetricsRegistry:
    def test_register_family(self):
        reg = MetricsRegistry()
        family = reg.register("test_counter", "counter", "A test counter")
        assert family.name == "test_counter"
        assert family.type == "counter"
        assert family.help == "A test counter"

    def test_counter_inc(self):
        reg = MetricsRegistry()
        reg.register("test_counter", "counter", "Test counter")
        reg.inc("test_counter", 1)
        assert reg._families["test_counter"].values[()] == 1.0
        reg.inc("test_counter", 2)
        assert reg._families["test_counter"].values[()] == 3.0

    def test_gauge_set(self):
        reg = MetricsRegistry()
        reg.register("test_gauge", "gauge", "Test gauge")
        reg.set("test_gauge", 42.0)
        assert reg._families["test_gauge"].values[()] == 42.0
        reg.set("test_gauge", 99.0)
        assert reg._families["test_gauge"].values[()] == 99.0

    def test_histogram_observe(self):
        reg = MetricsRegistry()
        reg.register("test_histogram", "histogram", "Test histogram")
        reg.observe("test_histogram", 1.5)
        reg.observe("test_histogram", 2.5)
        assert reg._families["test_histogram"].values[()] == 4.0  # sum, not bucket dist

    def test_labels(self):
        reg = MetricsRegistry()
        reg.register("breaker_trips", "counter", "Circuit breaker trips", ("breaker",))
        reg.inc("breaker_trips", 1, ("cost",))
        reg.inc("breaker_trips", 1, ("iteration",))
        assert reg._families["breaker_trips"].values[("cost",)] == 1.0
        assert reg._families["breaker_trips"].values[("iteration",)] == 1.0

    def test_prometheus_text(self):
        reg = MetricsRegistry()
        reg.register("test_counter", "counter", "Test counter")
        reg.inc("test_counter", 5)
        out = reg.prometheus_text()
        assert "# HELP test_counter Test counter" in out
        assert "# TYPE test_counter counter" in out
        assert "test_counter 5.0" in out or "test_counter 5" in out

    def test_prometheus_text_with_labels(self):
        reg = MetricsRegistry()
        reg.register("breaker", "counter", "Breaker trips", ("breaker_name",))
        reg.inc("breaker", 3, ("cost",))
        out = reg.prometheus_text()
        assert 'breaker_name="cost"' in out
        assert "breaker{" in out

    def test_missing_family_silent(self):
        reg = MetricsRegistry()
        reg.inc("nonexistent", 1)  # should not raise
        reg.set("nonexistent", 1)
        reg.observe("nonexistent", 1)


class TestInitConductorMetrics:
    def test_init_registers_metrics(self):
        init_conductor_metrics()
        reg = get_metrics_registry()
        assert "conductor_objectives_total" in reg._families
        assert "conductor_objectives_active" in reg._families
        assert "conductor_tasks_total" in reg._families
        assert "conductor_circuit_breaker_trips_total" in reg._families

    def test_get_returns_same(self):
        init_conductor_metrics()
        r1 = get_metrics_registry()
        r2 = get_metrics_registry()
        assert r1 is r2