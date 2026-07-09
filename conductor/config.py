"""Conductor configuration — matches Astatide gateway pattern.

Config precedence: CLI flags > env vars > YAML file > defaults
Env vars prefixed with CONDUCTOR_ using double-underscore nesting.
"""

import os
from dataclasses import dataclass, field, fields
from typing import Optional

import yaml
from pydantic import BaseModel


# ── Pydantic config models ────────────────────────────────────────────────


class ServiceConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8093
    mcp_path: str = "/mcp"


class AuthConfig(BaseModel):
    mode: str = "dev-none"  # dev-none | internal-only | cloudflare-access
    internal_secret: str = ""
    cloudflare_team_domain: str = ""
    cloudflare_aud: str = ""
    jwt_leeway_seconds: int = 30


class StorageConfig(BaseModel):
    sqlite_path: str = "./data/conductor.db"


class ObservabilityConfig(BaseModel):
    log_level: str = "INFO"
    log_format: str = "json"  # json | text
    metrics_enabled: bool = True


class AgentsGatewayClientConfig(BaseModel):
    url: str = "http://localhost:8092"
    auth_mode: str = "dev-none"
    internal_token: str = ""
    timeout_seconds: float = 30.0


class SkillsGatewayClientConfig(BaseModel):
    url: str = "http://localhost:8091"
    auth_mode: str = "dev-none"
    internal_token: str = ""
    timeout_seconds: float = 10.0


class McpGatewayClientConfig(BaseModel):
    """MCP Gateway as a downstream capability provider (external tool routing,
    connectors, GitHub/Drive/Calendar/mail). Conductor treats it as one of
    several downstream gateways — not as a parent of Conductor."""
    url: str = ""
    auth_mode: str = "dev-none"
    internal_token: str = ""
    timeout_seconds: float = 10.0


class WikiMcpClientConfig(BaseModel):
    """wiki-mcp downstream — durable memory, project context, decision logs.

    Disabled by default; opt in by setting CONDUCTOR_WIKI_MCP_URL and
    CONDUCTOR_WIKI_MCP_AUTH_MODE."""
    url: str = ""
    auth_mode: str = "dev-none"
    internal_token: str = ""
    timeout_seconds: float = 10.0


class CircuitConfig(BaseModel):
    max_iterations_per_run: int = 50
    max_cost_usd_per_run: float = 10.0
    max_concurrent_tasks: int = 4
    max_retries_per_task: int = 3
    max_wall_clock_minutes: int = 120
    max_stall_minutes: int = 30


class PlannerConfig(BaseModel):
    mode: str = "manual"  # manual | deterministic | llm
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 4096


class ConductorConfig(BaseModel):
    service: ServiceConfig = field(default_factory=ServiceConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    agents_gateway: AgentsGatewayClientConfig = field(default_factory=AgentsGatewayClientConfig)
    skills_gateway: SkillsGatewayClientConfig = field(default_factory=SkillsGatewayClientConfig)
    mcp_gateway: McpGatewayClientConfig = field(default_factory=McpGatewayClientConfig)
    wiki_mcp: WikiMcpClientConfig = field(default_factory=WikiMcpClientConfig)
    circuit: CircuitConfig = field(default_factory=CircuitConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    environment: str = "dev"


# ── Config loading ───────────────────────────────────────────────────────


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _load_yaml(path: str | None) -> dict:
    if path and os.path.isfile(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _env_overrides() -> dict:
    overrides: dict = {}
    for key, value in sorted(os.environ.items()):
        if key.startswith("CONDUCTOR_"):
            config_key = key[len("CONDUCTOR_") :].lower()
            parts = config_key.split("__")
            current = overrides
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = value
    return overrides


def _coerce_values(data: dict) -> dict:
    result: dict = {}
    for k, v in data.items():
        if isinstance(v, dict):
            result[k] = _coerce_values(v)
        elif isinstance(v, str):
            lv = v.lower()
            if lv in ("true", "1", "yes"):
                result[k] = True
            elif lv in ("false", "0", "no"):
                result[k] = False
            else:
                try:
                    result[k] = int(v)
                except ValueError:
                    try:
                        result[k] = float(v)
                    except ValueError:
                        result[k] = v
        else:
            result[k] = v
    return result


_CONDUCTOR_ENV = "CONDUCTOR_ENVIRONMENT"


def load_config(
    yaml_path: str | None = None,
    cli_host: str | None = None,
    cli_port: int | None = None,
) -> ConductorConfig:
    default = ConductorConfig().model_dump()

    yaml_data = _load_yaml(yaml_path)
    if yaml_data:
        default = _deep_merge(default, yaml_data)

    env_data = _coerce_values(_env_overrides())
    if env_data:
        default = _deep_merge(default, env_data)

    fallback = os.environ.get(_CONDUCTOR_ENV) or default.get("environment", "dev")
    default.setdefault("environment", fallback)

    cfg = ConductorConfig.model_validate(default)

    if cli_host is not None:
        cfg.service.host = cli_host
    if cli_port is not None:
        cfg.service.port = cli_port

    return cfg