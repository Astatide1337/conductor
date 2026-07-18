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


class ComposerConfig(BaseModel):
    enabled: bool = True
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 180.0
    max_parallel_tasks: int = 3
    max_repair_retries: int = 3
    poll_interval_seconds: float = 10.0
    default_harness_profile: str = "opencode-deepseek"
    integration_harness_profile: str = "opencode-deepseek"
    auto_start: bool = True
    auto_commit: bool = True
    auto_push: bool = False
    auto_pr: bool = False
    report_dir: str = "/var/lib/conductor/composer-reports"
    test_mode: bool = False  # when True, allows FakeComposerLLMClient and MockAgentsGatewayClient


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
    composer: ComposerConfig = field(default_factory=ComposerConfig)
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

    # Standard double-underscore nesting: CONDUCTOR_COMPOSER__LLM_BASE_URL → composer.llm_base_url
    for key, value in sorted(os.environ.items()):
        if key.startswith("CONDUCTOR_"):
            config_key = key[len("CONDUCTOR_") :].lower()
            parts = config_key.split("__")
            if len(parts) > 1:
                current = overrides
                for part in parts[:-1]:
                    current = current.setdefault(part, {})
                current[parts[-1]] = value
            elif config_key == "environment":
                overrides["environment"] = value

    # Backward-compatible single-underscore aliases (e.g. CONDUCTOR_COMPOSER_LLM_BASE_URL)
    _ALIASES = {
        "composer_llm_base_url": ("composer", "llm_base_url"),
        "composer_llm_api_key": ("composer", "llm_api_key"),
        "composer_llm_model": ("composer", "llm_model"),
        "composer_llm_timeout_seconds": ("composer", "llm_timeout_seconds"),
        "composer_enabled": ("composer", "enabled"),
        "composer_test_mode": ("composer", "test_mode"),
        "composer_max_parallel_tasks": ("composer", "max_parallel_tasks"),
        "composer_max_repair_retries": ("composer", "max_repair_retries"),
        "composer_poll_interval_seconds": ("composer", "poll_interval_seconds"),
        "composer_default_harness_profile": ("composer", "default_harness_profile"),
        "composer_integration_harness_profile": ("composer", "integration_harness_profile"),
        "composer_auto_start": ("composer", "auto_start"),
        "composer_report_dir": ("composer", "report_dir"),
        "agents_gateway_url": ("agents_gateway", "url"),
        "agents_gateway_auth_mode": ("agents_gateway", "auth_mode"),
        "agents_gateway_internal_token": ("agents_gateway", "internal_token"),
        "agents_gateway_timeout_seconds": ("agents_gateway", "timeout_seconds"),
        "skills_gateway_url": ("skills_gateway", "url"),
        "skills_gateway_auth_mode": ("skills_gateway", "auth_mode"),
        "skills_gateway_internal_token": ("skills_gateway", "internal_token"),
        "mcp_gateway_url": ("mcp_gateway", "url"),
        "mcp_gateway_auth_mode": ("mcp_gateway", "auth_mode"),
        "mcp_gateway_internal_token": ("mcp_gateway", "internal_token"),
        "wiki_mcp_url": ("wiki_mcp", "url"),
        "wiki_mcp_auth_mode": ("wiki_mcp", "auth_mode"),
        "wiki_mcp_internal_token": ("wiki_mcp", "internal_token"),
        "storage_sqlite_path": ("storage", "sqlite_path"),
        "service_host": ("service", "host"),
        "service_port": ("service", "port"),
    }
    for key, value in sorted(os.environ.items()):
        if not key.startswith("CONDUCTOR_"):
            continue
        config_key = key[len("CONDUCTOR_") :].lower()
        if "__" not in config_key and config_key in _ALIASES:
            path = _ALIASES[config_key]
            current = overrides
            for part in path[:-1]:
                current = current.setdefault(part, {})
            current[path[-1]] = value

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