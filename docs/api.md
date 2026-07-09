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
| `conductor_create_task` | Create a task under an objective's active run |
| `conductor_dispatch_task` | Dispatch a task via the shared Agents Gateway client (with skill validation) |
| `conductor_list_approvals` | List pending approvals |
| `conductor_approve` | Approve a pending approval |
| `conductor_reject` | Reject a pending approval |
| `conductor_reconcile` | Run reconciliation against the Agents Gateway |
| `conductor_view_events` | Inspect events with `objective_id`/`run_id`/`task_id`/`limit` filters |
| plus: `conductor_pause_objective`, `conductor_resume_objective`, `conductor_cancel_objective`, `conductor_get_objective_status`, `conductor_dry_run`, `conductor_health` | (legacy/utility tools) |