"""Tests for auth handler and middleware."""

import pytest
from starlette.testclient import TestClient

from conductor.auth import AuthHandler, AuthResult, _make_auth_middleware_cls
from conductor.config import AuthConfig, ConductorConfig
from conductor.server import create_app


class TestAuthHandler:
    def test_dev_none_always_allowed(self):
        handler = AuthHandler(AuthConfig(mode="dev-none"))
        result = handler.check("127.0.0.1")
        assert result.allowed
        assert result.user == "dev"

    def test_internal_only_no_secret_denies(self):
        handler = AuthHandler(AuthConfig(mode="internal-only", internal_secret=""))
        result = handler.check("127.0.0.1")
        assert not result.allowed

    def test_internal_only_wrong_token_denied(self):
        handler = AuthHandler(AuthConfig(mode="internal-only", internal_secret="s3cret"))
        result = handler.check("127.0.0.1", internal_token="wrong")
        assert not result.allowed

    def test_internal_only_correct_token_allowed(self):
        handler = AuthHandler(AuthConfig(mode="internal-only", internal_secret="s3cret"))
        result = handler.check("127.0.0.1", internal_token="s3cret")
        assert result.allowed
        assert result.user == "internal"

    def test_internal_only_no_token_denied(self):
        handler = AuthHandler(AuthConfig(mode="internal-only", internal_secret="s3cret"))
        result = handler.check("127.0.0.1", internal_token=None)
        assert not result.allowed

    def test_cloudflare_no_cf_denied(self):
        handler = AuthHandler(
            AuthConfig(mode="cloudflare-access", cloudflare_team_domain="test.cloudflareaccess.com")
        )
        result = handler.check("127.0.0.1")
        assert not result.allowed

    def test_cloudflare_with_jwt_allowed(self):
        handler = AuthHandler(
            AuthConfig(mode="cloudflare-access", cloudflare_team_domain="test.cloudflareaccess.com")
        )
        result = handler.check("127.0.0.1", cf_jwt="fake-jwt")
        assert result.allowed
        assert result.user == "cf"

    def test_cloudflare_internal_bypass(self):
        handler = AuthHandler(
            AuthConfig(
                mode="cloudflare-access",
                cloudflare_team_domain="test.cloudflareaccess.com",
                internal_secret="s3cret",
            )
        )
        result = handler.check("127.0.0.1", internal_token="s3cret")
        assert result.allowed
        assert result.user == "internal"

    def test_production_safety_internal_ok(self):
        handler = AuthHandler(
            AuthConfig(mode="internal-only", internal_secret="x"),
        )
        handler.require_production_safe()

    def test_production_safety_dev_none_raised_if_env(self, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_ENVIRONMENT", "production")
        handler = AuthHandler(AuthConfig(mode="dev-none"))
        with pytest.raises(RuntimeError, match="not allowed"):
            handler.require_production_safe()


class TestProtectedRoutesInInternalOnly:
    def test_health_always_public(self):
        cfg = ConductorConfig(
            environment="test",
            auth={"mode": "internal-only", "internal_secret": "s3cret"},
        )
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/health")
        assert r.status_code == 200

    def test_version_always_public(self):
        cfg = ConductorConfig(
            environment="test",
            auth={"mode": "internal-only", "internal_secret": "s3cret"},
        )
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/version")
        assert r.status_code == 200

    def test_protected_denied_without_token(self):
        cfg = ConductorConfig(
            environment="test",
            auth={"mode": "internal-only", "internal_secret": "s3cret"},
        )
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/objectives")
        assert r.status_code == 401

    def test_protected_allowed_with_token(self):
        cfg = ConductorConfig(
            environment="test",
            auth={"mode": "internal-only", "internal_secret": "s3cret"},
        )
        app = create_app(cfg)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/objectives", headers={"X-Auth-Internal-Token": "s3cret"})
        assert r.status_code != 401  # maybe 501, but auth passes