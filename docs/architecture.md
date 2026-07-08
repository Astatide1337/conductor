# Astatide Conductor — Architecture

## Position in the platform

```
Any MCP-capable cockpit
      |
      v
MCP Gateway
      |
      v
Conductor ───── objective orchestration, task graph, policy, approvals
      |
      v
Agents Gateway ─── task execution, runtime adapters, artifacts
      |
      v
Skills Gateway ─── methodology, skills, durable memory
```

## Responsibility boundaries

| Component | Owns |
|---|---|
| **Conductor** | Objectives, task graph, planner decisions, approvals, policy, circuit breakers, cost accounting, dispatch coordination, status aggregation, reconciliation, MCP cockpit surface |
| **Agents Gateway** | Agent/task execution, runtime adapters, background workers, task events, task artifacts, runtime sandboxing |
| **Skills Gateway** | Skills, methodology, skill metadata, skill reading |
| **MCP Gateway** | External MCP access, cockpit connectivity, routing |

## Internal architecture

```txt
conductor/
  cli.py          — Typer CLI (run, version, doctor)
  config.py       — Pydantic config (CLI > env > YAML > defaults)
  server.py       — FastAPI app + FastMCP MCP tools mount
  auth.py         — Auth handler (dev-none, internal-only, cloudflare-access)
  storage.py      — SQLite with state machine validation
  models.py       — Pydantic domain models
  policy.py       — action verdict engine + agent output safety
  circuit.py      — circuit breakers and BreakerEvaluator
  events.py       — append-only event audit trail
  dispatch.py     — idempotent task dispatch to Agents Gateway
  metrics.py      — Prometheus metrics registry
  logging.py      — structured JSON logging with contextvars
  mcp_tools.py    — 15 MCP cockpit tools
  planner/
    deterministic.py — rule-based planner
    llm.py           — LLM planner adapter (future)
  clients/
    agents_gateway.py  — Mock + HTTP clients for Agents Gateway
    skills_gateway.py  — Mock + HTTP clients for Skills Gateway
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
- **MCP Gateway**: Exposes conductor MCP tools to cockpits

## Key design choices

1. **SQLite with WAL** — durable, portable, restart-safe
2. **Idempotent dispatch** — `obj:run:task:attempt` keys prevent duplicates
3. **Reconciliation loop** — repair state after restart
4. **Policy layer** — explicit verdicts, not prompt-based
5. **Manual-first** — usable without LLM from day 1