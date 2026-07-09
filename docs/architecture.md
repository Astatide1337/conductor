# Astatide Conductor — Architecture

## Position in the platform

Conductor is the **single hub** that MCP-capable cockpits connect to.
Gateways (Agents, Skills, MCP, wiki, future) are downstream capability
providers — the MCP Gateway is one of them, not the parent of Conductor.

```
Any MCP-compatible cockpit (Claude / ChatGPT / CLI / future UI)
       |
       v
Conductor ─── single MCP + REST surface (the hub)
       |
   +---+---+---+---+---------+
   |   |   |   |   |         |
   v   v   v   v   v         v
Agents Skills MCP wiki  future gateways (mail / calendar / cloud / deploy / …)
```

Conductor **owns** the orchestration spine, gateway registry, health
probes, capability catalog, dispatch / reconciliation, and the unified
objective timeline. Cockpits ask "What happened with objective X?" and
get one answer — they never need to inspect each gateway directly.

## Responsibility boundaries

| Component | Owns |
|---|---|
| **Conductor** | Objectives, task graph, planner decisions, approvals, policy, circuit breakers, cost accounting, dispatch coordination, status aggregation, reconciliation, MCP cockpit surface, **Gateway Hub** (registry, health probes, capability catalog, unified timeline) |
| **Agents Gateway** | Agent/task execution, runtime adapters, background workers, task events, task artifacts, runtime sandboxing |
| **Skills Gateway** | Skills, methodology, skill metadata, skill reading |
| **MCP Gateway** | External MCP tools / connectors (GitHub, Drive, Calendar, mail). Conductor treats MCP Gateway as one of several downstream capability providers — not as its parent. |
| **wiki-mcp** | Durable memory, project context, decision logs |

Conductor is the single hub. Cockpits connect only to Conductor; all
downstream gateways are reached by Conductor on the cockpit's behalf.

## Internal architecture

```txt
conductor/
  cli.py          — Typer CLI (run, version, doctor)
  config.py       — Pydantic config (CLI > env > YAML > defaults)
  server.py       — FastAPI app + FastMCP MCP tools mount; gateway hub routes
  auth.py         — Auth handler (dev-none, internal-only, cloudflare-access)
  storage.py      — SQLite with state machine validation
  models.py       — Pydantic domain models
  policy.py       — action verdict engine + agent output safety
  circuit.py      — circuit breakers and BreakerEvaluator
  events.py       — append-only event audit trail
  dispatch.py     — idempotent task dispatch to Agents Gateway
                    (skills gate + capabilities gate, both pre-transition)
  metrics.py      — Prometheus metrics registry (incl. gateway hub gauges)
  logging.py      — structured JSON logging with contextvars
  mcp_tools.py    — 23 MCP cockpit tools (objective/task/approval +
                    gateway-hub: list/check/status/capabilities/timeline)
  gateways/       — Gateway Hub package
    models.py     — GatewayConfig + GatewayStatus
    registry.py   — GatewayRegistry + build_default_registry(cfg)
    health.py     — check_gateway_health / check_all_gateways
    capabilities.py — static capability catalog by gateway kind
    validation.py — validate_required_capabilities +
                    get_required_capabilities_from_task
    events.py     — gateway.* event emitters
  planner/
    deterministic.py — rule-based planner
    llm.py           — LLM planner adapter (future)
  clients/
    agents_gateway.py  — Mock + HTTP clients for Agents Gateway
    skills_gateway.py  — Mock + HTTP clients for Skills Gateway
    mcp_gateway.py     — Mock + HTTP clients for MCP Gateway (downstream)
```

## Data model

8 SQLite tables with strict state machine transitions:

| Table | Purpose |
|---|---|
| `objectives` | User-level goals with status lifecycle |
| `objective_runs` | Concrete execution attempts (supports retry) |
| `tasks` | Conductor-level units of work |
| `agent_runs` | Tracks Agents Gateway task executions |
| `approvals` | Human approval queue |
| `events` | Append-only audit trail |
| `planner_turns` | Planner invocation records |
| `cost_ledger` | Cost and token tracking |

### State machines

**Objective**: created → active → {paused, blocked, completed, failed, cancelled}

**Task**: created → ready → dispatched → running → {completed, failed, blocked, cancelled}

**Agent run**: created → dispatched → queued → running → {completed, failed, cancelled, lost}

## Planner modes

| Mode | Description |
|---|---|
| `manual` | Human submits structured decisions via cockpit |
| `deterministic` | Rule-based automation (dispatch ready tasks, complete objectives, retry failed) |
| `llm` | LLM proposes structured decisions (future milestone) |

## Circuit breakers

6 hard safety limits per run. When tripped, objective is paused/blocked and
escalation approval is created.

## Integration points

- **Agents Gateway**: HTTP REST at `/tasks`, `/agents`, `/inventory`
- **Skills Gateway**: HTTP REST at `/skills`, `/inventory` or MCP tools
- **MCP Gateway**: HTTP `/health`, `/version`, `POST /tools/list`, `POST /tools/call`
  (treated as a **downstream** provider so any cockpit connecting to
  Conductor can reach MCP Gateway tools without connecting to MCP
  Gateway directly)
- **wiki-mcp**: HTTP `/health`, `/version` (and memory endpoints as the
  gateway exposes them)

## Key design choices

1. **SQLite with WAL** — durable, portable, restart-safe
2. **Idempotent dispatch** — `obj:run:task:attempt` keys prevent duplicates
3. **Reconciliation loop** — repair state after restart
4. **Policy layer** — explicit verdicts, not prompt-based
5. **Manual-first** — usable without LLM from day 1