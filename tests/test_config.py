"""Tests for config loading following the Astatide gateway pattern."""

import os
import tempfile

import pytest
import yaml

from conductor.config import ConductorConfig, load_config, _deep_merge, _env_overrides, _coerce_values


class TestDeepMerge:
    def test_flat_merge(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"srv": {"host": "0.0.0.0", "port": 8093}}
        override = {"srv": {"port": 9000}, "env": "prod"}
        result = _deep_merge(base, override)
        assert result["srv"]["host"] == "0.0.0.0"
        assert result["srv"]["port"] == 9000
        assert result["env"] == "prod"

    def test_deep_nested_merge(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"d": 3, "e": 4}}}
        result = _deep_merge(base, override)
        assert result["a"]["b"]["c"] == 1
        assert result["a"]["b"]["d"] == 3
        assert result["a"]["b"]["e"] == 4


class TestCoerceValues:
    def test_bool_true(self):
        assert _coerce_values({"enabled": "true"}) == {"enabled": True}
        assert _coerce_values({"x": "1"}) == {"x": True}
        assert _coerce_values({"x": "yes"}) == {"x": True}

    def test_bool_false(self):
        assert _coerce_values({"enabled": "false"}) == {"enabled": False}
        assert _coerce_values({"x": "0"}) == {"x": False}
        assert _coerce_values({"x": "no"}) == {"x": False}

    def test_int_float(self):
        assert _coerce_values({"port": "8093"}) == {"port": 8093}
        assert _coerce_values({"cost": "10.5"}) == {"cost": 10.5}

    def test_string(self):
        assert _coerce_values({"name": "conductor"}) == {"name": "conductor"}

    def test_nested(self):
        result = _coerce_values({"svc": {"port": "8093", "enabled": "true"}})
        assert result == {"svc": {"port": 8093, "enabled": True}}


class TestEnvOverrides:
    def test_prefixed_vars(self, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_SERVICE__PORT", "9000")
        monkeypatch.setenv("CONDUCTOR_ENVIRONMENT", "staging")
        monkeypatch.setenv("NOT_CONDUCTOR", "ignored")
        overrides = _env_overrides()
        assert overrides["service"]["port"] == "9000"  # raw string, coercion happens later
        assert overrides["environment"] == "staging"
        assert "not_conductor" not in overrides

    def test_nested_env_keys(self, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_AUTH__MODE", "internal-only")
        monkeypatch.setenv("CONDUCTOR_CIRCUIT__MAX_CONCURRENT_TASKS", "8")
        overrides = _env_overrides()
        assert overrides["auth"]["mode"] == "internal-only"
        assert overrides["circuit"]["max_concurrent_tasks"] == "8"


class TestConfigDefaults:
    def test_default_config(self):
        cfg = ConductorConfig()
        assert cfg.service.host == "0.0.0.0"
        assert cfg.service.port == 8093
        assert cfg.auth.mode == "dev-none"
        assert cfg.storage.sqlite_path == "./data/conductor.db"
        assert cfg.planner.mode == "manual"
        assert cfg.environment == "dev"
        assert cfg.circuit.max_concurrent_tasks == 4

    def test_load_defaults(self):
        cfg = load_config()
        assert isinstance(cfg, ConductorConfig)
        assert cfg.service.port == 8093


class TestYamlLoading:
    def test_load_yaml_overrides(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"service": {"port": 9000}, "environment": "staging"}, f)
            path = f.name
        try:
            cfg = load_config(yaml_path=path)
            assert cfg.service.port == 9000
            assert cfg.environment == "staging"
            assert cfg.service.host == "0.0.0.0"  # default preserved
        finally:
            os.unlink(path)


class TestCliOverrides:
    def test_cli_host_port(self):
        cfg = load_config(cli_host="127.0.0.1", cli_port=17000)
        assert cfg.service.host == "127.0.0.1"
        assert cfg.service.port == 17000


class TestPrecedence:
    def test_env_overrides_yaml(self, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_SERVICE__PORT", "9999")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"service": {"port": 8888}}, f)
            path = f.name
        try:
            cfg = load_config(yaml_path=path)
            assert cfg.service.port == 9999  # env wins
        finally:
            os.unlink(path)

    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_SERVICE__PORT", "9999")
        cfg = load_config(cli_port=7777)
        assert cfg.service.port == 7777  # CLI wins


class TestProductionSafety:
    def test_dev_none_in_production_refused(self, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_ENVIRONMENT", "production")
        cfg = ConductorConfig(auth={"mode": "dev-none"})
        from conductor.auth import AuthHandler

        with pytest.raises(RuntimeError, match="not allowed"):
            AuthHandler(cfg.auth).require_production_safe()

    def test_internal_only_in_production_ok(self):
        cfg = ConductorConfig(auth={"mode": "internal-only", "internal_secret": "x"}, environment="production")
        from conductor.auth import AuthHandler

        AuthHandler(cfg.auth).require_production_safe()  # should not raise