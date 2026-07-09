# Gateway Hub

Conductor is the single hub that MCP-capable cockpits connect to. All
downstream gateways (Agents, Skills, MCP, wiki, future ones) are
**capability providers** Conductor talks to. Cockpits do **not** talk to
gateways directly.

```
Any MCP-compatible cockpit / client (Claude Desktop, Polychat, CLI, future UI)
        |
        v
   CONDUCTOR  ─── single MCP + REST surface
        |
   +----+----+----+----+---------+
   |    |    |    |    |          |
   v    v    v    v    v          v
Agents  Skills MCP  wiki  custom future gateways
Gateway Gateway Gateway mcp
```

Conductor's Gateway Hub layer lets operators and cockpits answer:

1. Which gateways exist?
2. Are they healthy?
3. What capabilities does each expose?
4. Which auth mode does each use?
5. Which tasks depend on which gateways?
6. What happened with objective X? (timeline)

## Architecture correction

> The previous architecture diagram modeled the system as
> `Client → MCP Gateway → Conductor → Agents Gateway → Skills Gateway`.
> That is **wrong** for this milestone. The corrected model is:
>
> `Client → Conductor → all downstream gateways (Agents, Skills, MCP, wiki, …)`

MCP Gateway is now just one of several downstream capability providers
Conductor orchestrates — not a parent of Conductor.

## Gateway kinds

| Kind | Purpose | Enabled by default |
|---|---|---|
| `agents` | Task execution, runtimes, artifacts | yes |
| `skills` | Reusable skills, methodology, validation | yes |
| `mcp` | External tools, connectors, GitHub/Drive/Calendar/mail | only when `CONDUCTOR_MCP_GATEWAY_URL` is set |
| `wiki` | Durable memory, project context, decision logs | only when `CONDUCTOR_WIKI_MCP_URL` is set |
| `custom` | Future / bespoke gateways | per-gateway |

## Environment variables

Existing variables (Agents + Skills Gateways) continue to work. New
variables are required to expose the MCP Gateway and wiki-mcp surfaces.

### MCP Gateway (NEW)

```bash
export CONDUCTOR_MCP_GATEWAY__URL=https://mcp.astatide.com
export CONDUCTOR_MCP_GATEWAY__AUTH_MODE=internal-only
export CONDUCTOR_MCP_GATEWAY__INTERNAL_TOKEN=<token>
# Optional: timeout
export CONDUCTOR_MCP_GATEWAY__TIMEOUT_SECONDS=10
```

For the live E2E script the env-var names are flattened (no
double-underscore):

```bash
export CONDUCTOR_MCP_GATEWAY_URL=...
export CONDUCTOR_MCP_GATEWAY_AUTH_MODE=internal-only
export CONDUCTOR_MCP_GATEWAY_INTERNAL_TOKEN=...
```

### wiki-mcp (NEW, optional)

```bash
export CONDUCTOR_WIKI_MCP__URL=https://wiki.astatide.com
export CONDUCTOR_WIKI_MCP__AUTH_MODE=internal-only
export CONDUCTOR_WIKI_MCP__INTERNAL_TOKEN=<token>
```

When `CONDUCTOR_WIKI_MCP__URL` is unset, the wiki gateway is registered
but reported as `not_configured` so operators can see "what would become
available if we enabled this gateway".

## Registry

`conductor/gateways/registry.py::build_default_registry(cfg)` constructs
the canonical four-gateway registry (agents, skills, mcp, wiki) on every
server start. The registry is purely **configuration** — URLs, auth mode,
enabled flag, health/version paths. It never carries tokens (those stay
in the HTTP clients).

A `gateway.registered` event is emitted for each gateway at startup so
the operator timeline reflects the hub at boot.

## Health behavior

Status rules (in order, applied by
`conductor/gateways/health.py::check_gateway_health`):

| Condition | Resulting status |
|---|---|
| Empty `base_url` | `not_configured` |
| `enabled=False` | `disabled` |
| `/health` 2xx | `healthy` (also probes `/version` best-effort) |
| HTTP 401 / 403 | `auth_failed` |
| `httpx.TimeoutException` | `timeout` |
| HTTP 5xx | `unhealthy` |
| Other HTTP 4xx | `unhealthy` |
| Transport error | `unhealthy` |
| Unexpected exception | `error` |

A `GatewayStatus` carries: `id`, `kind`, `name`, `enabled`,
`configured`, `healthy`, `status`, `base_url_present`, `auth_mode`,
`version` (best-effort), `capabilities` (the static capability list for
this gateway kind), `last_checked_at`, `latency_ms`, and `error`.

Health checks never raise — every failure becomes a structured status
object — so a single broken downstream gateway cannot crash the
`/gateways/check-all` route.

## Capability discovery

Static capability catalog by gateway kind, in
`conductor/gateways/capabilities.py::STATIC_CAPABILITIES`:

| Kind | Capabilities |
|---|---|
| agents | `execution.task.create`, `execution.task.run`, `execution.task.status`, `execution.events.read`, `execution.artifacts.read` |
| skills | `skills.list`, `skills.inspect`, `skills.validate`, `skills.read` |
| mcp | `tools.list`, `tools.call`, `connectors.route`, `external.github`, `external.drive`, `external.calendar`, `external.mail` |
| wiki | `memory.read`, `memory.write`, `memory.search`, `context.project` |

A capability is `available=True` when its gateway is configured (URL
present) AND enabled. Capabilities for not-configured or disabled
gateways are still listed (with `available=False`) so operators can see
"what would become available if we enabled this gateway".

## Task capability validation

A task may declare `required_capabilities` in its `metadata`. When
Conductor dispatches the task, the dispatch path runs a capability gate
**after** the skills gate and **before** any state transition /
agent_run row / gateway call:

```
dispatch_task =
  1. validate skills   → block on missing
  2. validate capabilities → block on missing (or degraded if require_healthy=True)
  3. record dispatch_requested
  4. transition task → agent_run → gateway call
```

When capabilities are missing, dispatch returns:

```json
{
  "task_id": "...",
  "status": "ready",
  "agent_run": null,
  "error": "missing required capabilities: ['execution.task.create']",
  "missing_capabilities": [...],
  "degraded_capabilities": [...],
  "satisfied_capabilities": [...]
}
```

The task remains in its original state. Two events are emitted:

* `task.capabilities_validation_failed` (when gate fails)
* `task.capabilities_validated` (when gate passes)

## HTTP API (new)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/gateways` | Yes | List configured gateways (no live probe) |
| GET | `/gateways/status` | Yes | Lightweight statuses derived from config (no probe) |
| GET | `/gateways/{id}` | Yes | Get a single gateway config |
| POST | `/gateways/{id}/check` | Yes | Live-probe a single gateway |
| POST | `/gateways/check-all` | Yes | Live-probe every configured gateway |
| GET | `/capabilities` | Yes | List all known capabilities (optional `?gateway_id=`) |
| GET | `/capabilities/{cap}` | Yes | Find candidate gateways that provide a capability |
| GET | `/objectives/{id}/timeline` | Yes | Chronological timeline of every event for an objective |

`/gateways` and `/gateways/status` intentionally do not perform live
probes — they're meant to be cheap operator views, callable the moment
Conductor starts. Use `POST /gateways/check-all` or
`POST /gateways/{id}/check` for live health.

## MCP cockpit tools (new)

Conductor's MCP surface adds the following high-level tools. All of them
return clean JSON-serializable objects; tokens are never exposed.

| Tool | Description |
|---|---|
| `conductor_list_gateways` | List configured gateways (no probe) |
| `conductor_get_gateway_status` | Single gateway lightweight status |
| `conductor_check_gateway_health` | Live-probe one gateway |
| `conductor_check_all_gateways` | Live-probe every gateway |
| `conductor_list_capabilities` | List all capabilities (filterable) |
| `conductor_find_capability` | Find candidate gateways for a capability |
| `conductor_call_mcp_gateway_tool` | **EXPERIMENTAL** — call a single MCP Gateway tool. Marked experimental because routing arbitrary gateway tools to end-user cockpits without policy is unsafe. Use with care; Conductor only records the tool name in the audit timeline (arguments are not logged verbatim). |
| `conductor_get_timeline` | Chronological timeline of all events for an objective |

## Timeline / operator view

The single most important endpoint for the cockpit experience is
`GET /objectives/{objective_id}/timeline`. It returns every event for
that objective in chronological order so a cockpit can answer "What
happened with objective X?" without needing to inspect each gateway
individually.

Events include:

```
objective.created
task.created
task.skills_validated
task.capabilities_validated
task.dispatch_requested
gateway.agents.dispatch
agent_run.created
agent_run.reconciled
artifacts.ingested
gateway.health_checked
gateway.health_failed
approval.requested
approval.approved
approval.rejected
```

The companion MCP tool is `conductor_get_timeline`.

## Metrics

New counters / gauges registered in `conductor/metrics.py` and exposed at
`/metrics`:

| Metric | Type | Description |
|---|---|---|
| `conductor_gateways_total` | gauge | Registered gateways |
| `conductor_gateways_healthy` | gauge | Healthy gateways (latest `check-all`) |
| `conductor_gateways_unhealthy` | gauge | Unhealthy gateways (latest `check-all`) |
| `conductor_gateway_health_checks_total` | counter | Gateway health checks performed |
| `conductor_gateway_health_check_errors_total` | counter | Health checks that errored (auth_failed / timeout / unhealthy / error) |
| `conductor_capability_validation_total` | counter | Task capability validations performed |
| `conductor_capability_validation_failed_total` | counter | Tasks whose capability gate failed |
| `conductor_gateway_actions_total{gateway_kind="..."}` | counter | Gateway actions dispatched by kind |

## Local vs live verification

| Script | Purpose |
|---|---|
| `scripts/e2e-local.sh` | 15-step offline smoke (pre-existing) |
| `scripts/e2e-local-gateway-hub.sh` | 17-step offline smoke covering list gateways, check-all, capabilities, dispatch, reconcile, timeline, MCP tools |
| `scripts/e2e-live-agents.sh` | Live Agents Gateway + Skills Gateway smoke (existing) |
| `scripts/e2e-live-gateway-hub.sh` | **NEW** Live Agents + Skills + MCP Gateway + (optional) wiki smoke. Exits 2 if any required env var is missing. |

Live E2E refuses to fake credentials. Missing required environment
variables cause exit code `2` with the exact missing names printed.

## Known limitations

- LLM planner is **not** built; deferred.
- Autonomous loop mode is **not** built; deferred.
- Harness/tmux runtime is **not** built; deferred.
- MCP Gateway downstream client supports the standard `health`,
  `version`, `tools/list`, `tools/call` methods. If a particular MCP
  Gateway deployment exposes an alternate surface, the HTTP client can
  be adjusted without changing callers.
- Live E2E depends on credentials in the environment. The script
  reports missing variables and exits 2; it does not fake a run.
- Capability catalog is **static per gateway kind** for this milestone.
  A future milestone may add dynamic capability discovery (e.g.,
  reading `/capabilities` from a downstream gateway when it exposes one).
