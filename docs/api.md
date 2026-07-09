# Conductor HTTP API Reference

All protected routes require authentication in non-dev-none modes.
See SECURITY.md for auth details.

## Public endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | No | `{"status":"ok","service":"astatide-conductor"}` |
| GET | `/ready` | No | Readiness check including storage, auth, planner |
| GET | `/version` | No | Service name, version, environment |
| GET | `/metrics` | Protected | Prometheus text metrics |

## Objectives

| Method | Path | Status | Description |
|---|---|---|---|
| POST | `/objectives` | 201 | Create objective + initial run |
| GET | `/objectives` | 200 | List objectives (optional `?status=`, `?limit=`, `?offset=`) |
| GET | `/objectives/{id}` | 200 | Get objective with latest run |
| POST | `/objectives/{id}/pause` | 200 | Pause objective (auto-activates if created) |
| POST | `/objectives/{id}/resume` | 200 | Resume from paused/created |
| POST | `/objectives/{id}/cancel` | 200 | Cancel objective |
| GET | `/events` | 200 | List events (filters: `?objective_id=`, `?run_id=`, `?task_id=`) |

### Create objective

```bash
curl -X POST http://localhost:8093/objectives \
  -H "Content-Type: application/json" \
  -d '{"title":"Add auth module","description":"Build oauth flow","priority":"high"}'
```

Response (201):
```json
{
  "objective_id": "uuid",
  "run_id": "uuid",
  "status": "created"
}
```

## Tasks

| Method | Path | Status | Description |
|---|---|---|---|
| POST | `/objectives/{id}/tasks` | 201 | Create task on objective's active run |
| GET | `/tasks` | 200 | List tasks (filters: `?objective_id=`, `?run_id=`, `?status=`) |
| GET | `/tasks/{id}` | 200 | Get task with agent runs |
| POST | `/tasks/{id}/dispatch` | 200 | Dispatch task to Agents Gateway |

### Create task

```bash
curl -X POST http://localhost:8093/objectives/{obj_id}/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"Build auth handler","task_type":"ship","required_skills":["code-review"]}'
```

### Dispatch task

```bash
curl -X POST http://localhost:8093/tasks/{task_id}/dispatch
```

Response (200):
```json
{
  "id": "agent-run-uuid",
  "task_id": "task-uuid",
  "agents_gateway_task_id": "gw-task-1",
  "status": "running",
  "idempotency_key": "obj:run:task:1"
}
```

## Approvals

| Method | Path | Status | Description |
|---|---|---|---|
| GET | `/approvals` | 200 | List pending approvals (filters: `?objective_id=`, `?status=`) |
| POST | `/approvals/{id}/approve` | 200 | Approve a pending approval |
| POST | `/approvals/{id}/reject` | 200 | Reject a pending approval |

## Reconciliation and dry run

| Method | Path | Status | Description |
|---|---|---|---|
| POST | `/reconcile` | 200 | Reconcile all in-flight agent_runs against the Agents Gateway |
| POST | `/dry-run` | 200 | Deterministic dry-run analysis |
```bash
curl -X POST http://localhost:8093/reconcile
```

Response (200):
```json
{
  "reconciled": 4,
  "transitions": 2,
  "errors": 0,
  "by_target": {"completed": 1, "failed": 1},
  "candidate_count": 4
}
```

- `reconciled` — agent_runs for which the reconcile call succeeded (success or
  no-op).
- `transitions` — count whose `status` actually changed.
- `errors` — reconcile_task exceptions (e.g., gateway unreachable). Each
  failure increments `conductor_reconciliation_errors_total`.
- `by_target` — histogram of post-reconcile statuses.
- `candidate_count` — number of in-flight agent_runs considered.

### Dry run

```bash
curl -X POST http://localhost:8093/dry-run
```

Response (200):
```json
{
  "proposed_tasks": [],
  "required_skills": [],
  "approval_gates": [],
  "estimated_risks": ["2 ready tasks pending dispatch"],
  "would_dispatch": false
}
```

## Dispatch — skill validation gate

`POST /tasks/{id}/dispatch` validates `required_skills` against the
configured Skills Gateway **before** any state transition, agent_run row, or
gateway call.

If skills are missing, the caller receives a **structured error** (still
HTTP 200) and the task is left in its original state. No `agent_run` row is
created. The Agents Gateway is never called.

Response when skills are missing (200):
```json
{
  "task_id": "uuid",
  "status": "ready",
  "agent_run": null,
  "error": "missing required skills: ['never-exists']",
  "missing_skills": ["never-exists"],
  "validated_skills": ["pytest-mcp"]
}
```

The `task.skills_validation_failed` event is emitted with `payload.original_status`
and `payload.missing_skills`.

## Dispatch — capability validation gate

After the skill gate, if the task's `metadata.required_capabilities` is
non-empty, Conductor runs the **capability gate**: each required
capability is checked against the Gateway Hub registry to ensure at
least one configured+enabled gateway provides it. Missing capabilities
block dispatch:

Response when capabilities are missing (200):
```json
{
  "task_id": "uuid",
  "status": "ready",
  "agent_run": null,
  "error": "missing required capabilities: ['execution.task.create']",
  "missing_capabilities": ["execution.task.create"],
  "degraded_capabilities": [],
  "satisfied_capabilities": []
}
```

The `task.capabilities_validation_failed` event is emitted with payload
`{missing_capabilities, degraded_capabilities, satisfied_capabilities,
original_status}`. When the gate passes, `task.capabilities_validated` is
emitted.

Capability catalog and standard capability strings are documented in
`docs/gateway-hub.md`.

## Gateway Hub

| Method | Path | Status | Description |
|---|---|---|---|
| GET | `/gateways` | 200 | List configured gateways (no live probe) |
| GET | `/gateways/status` | 200 | Lightweight status snapshot (no live probe) |
| GET | `/gateways/{gateway_id}` | 200 | Get a single gateway config |
| POST | `/gateways/{gateway_id}/check` | 200 | Live-probe one gateway |
| POST | `/gateways/check-all` | 200 | Live-probe every configured gateway |
| GET | `/capabilities` | 200 | List all capabilities (`?gateway_id=` optional filter) |
| GET | `/capabilities/{capability}` | 200 | Find candidate gateways that provide a capability |

### List gateways

```bash
curl -X GET http://localhost:8093/gateways
```

Response (200):
```json
{
  "gateways": [
    {"id":"agents","kind":"agents","name":"Agents Gateway","enabled":true,"configured":true,...},
    {"id":"skills","kind":"skills","name":"Skills Gateway","enabled":true,"configured":true,...},
    {"id":"mcp","kind":"mcp","name":"MCP Gateway","enabled":false,"configured":false,...},
    {"id":"wiki","kind":"wiki","name":"wiki-mcp","enabled":false,"configured":false,...}
  ],
  "count": 4
}
```

### Check all gateways

```bash
curl -X POST http://localhost:8093/gateways/check-all
```

Response (200):
```json
{
  "statuses": [
    {"id":"agents","kind":"agents","name":"Agents Gateway",
     "enabled":true,"configured":true,"healthy":true,"status":"healthy",
     "base_url_present":true,"auth_mode":"internal-only",
     "version":"1.0.0","capabilities":["execution.task.create",...],
     "last_checked_at":"2026-07-09T...","latency_ms":12.4,"error":null}
  ],
  "count": 4
}
```

Each status includes the static capability list for its gateway kind, so
operators can answer "What can Conductor do, and via which gateway?" from
one call.

### Find capability

```bash
curl -X GET http://localhost:8093/capabilities/execution.task.create
```

Response (200):
```json
{
  "capability": "execution.task.create",
  "candidates": [
    {"capability":"execution.task.create","gateway_id":"agents",
     "gateway_kind":"agents","available":true,"source":"static",
     "description":"Create execution tasks through Agents Gateway."}
  ],
  "count": 1
}
```

## Objective timeline

| Method | Path | Status | Description |
|---|---|---|---|
| GET | `/objectives/{objective_id}/timeline` | 200 | Chronological order of all events for an objective |

```bash
curl -X GET http://localhost:8093/objectives/{objective_id}/timeline
```

Response (200):
```json
{
  "objective_id": "uuid",
  "count": 12,
  "events": [
    {"event_type":"objective.created","created_at":"...","message":"..."},
    {"event_type":"task.created","...":"..."},
    {"event_type":"task.skills_validated","...":"..."},
    {"event_type":"task.capabilities_validated","...":"..."},
    {"event_type":"task.dispatch_requested","...":"..."},
    {"event_type":"gateway.agents.dispatch","...":"..."},
    {"event_type":"agent_run.created","...":"..."},
    {"event_type":"agent_run.reconciled","...":"..."},
    {"event_type":"artifacts.ingested","...":"..."}
  ]
}
```

Cockpits use this as the "What happened with objective X?" entry point —
no need to inspect each downstream gateway individually.

## Authentication

Protected routes require one of:

- `dev-none` mode — no header needed (dev only)
- `internal-only` mode — `X-Auth-Internal-Token: <secret>` header
- `cloudflare-access` mode — `Cf-Access-Jwt-Assertion: <jwt>` header

Public routes: `/health`, `/ready`, `/version` never require auth.

## MCP cockpit surface at `/mcp`

The MCP path exposes Conductor's high-level cockpit tools (see
`conductor/mcp_tools.py`). The auth model is the same as the REST API:

- Unauthenticated requests return `401` **with a JSON-RPC 2.0 envelope**:
  ```json
  {"jsonrpc":"2.0","error":{"code":-32001,"message":"missing internal token"},"id":null}
  ```
  This is distinct from unauthenticated REST traffic, which returns
  `{"detail": "..."}` — a deliberate choice so MCP cockpits can parse
  rejections cleanly.
- Authenticated requests proceed normally; `initialize`, `tools/list`, and
  `tools/call` all require the same auth header(s) as REST.

The full tool list:

| Tool | Description |
|---|---|
| `conductor_create_objective` | Create an objective + initial run |
| `conductor_get_objective` | Get objective with runs |
| `conductor_list_objectives` | List objectives (filters: `?status=`, `?limit=`) |
| `conductor_get_status` | Comprehensive status (objective/run/tasks/approvals/circuit-breakers) |
| `conductor_create_task` | Create a task under an objective's active run (accepts `required_capabilities_json`) |
| `conductor_dispatch_task` | Dispatch a task via the shared Agents Gateway client (with skill + capability validation) |
| `conductor_list_approvals` | List pending approvals |
| `conductor_approve` | Approve a pending approval |
| `conductor_reject` | Reject a pending approval |
| `conductor_steer_objective` | Add steering guidance to an objective |
| `conductor_pause_objective` | Pause an objective |
| `conductor_resume_objective` | Resume a paused/created objective |
| `conductor_cancel_objective` | Cancel an objective |
| `conductor_dry_run` | Deterministic dry-run analysis |
| `conductor_reconcile` | Run reconciliation against the Agents Gateway |
| `conductor_view_events` | Inspect events with `objective_id`/`run_id`/`task_id`/`limit` filters |
| `conductor_health_check` | Conductor service health + planner + auth mode |
| `conductor_list_gateways` | **New** — list configured gateways (no probe) |
| `conductor_get_gateway_status` | **New** — single gateway lightweight status |
| `conductor_check_gateway_health` | **New** — live-probe one gateway |
| `conductor_check_all_gateways` | **New** — live-probe every gateway |
| `conductor_list_capabilities` | **New** — list capabilities across the hub |
| `conductor_find_capability` | **New** — find candidate gateways for a capability |
| `conductor_call_mcp_gateway_tool` | **New, EXPERIMENTAL** — invoke a downstream MCP Gateway tool by name |
| `conductor_get_timeline` | **New** — chronological timeline for an objective |