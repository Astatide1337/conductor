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
| POST | `/reconcile` | 501 | Not yet implemented |
| POST | `/dry-run` | 200 | Deterministic dry-run analysis |

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

## Authentication

Protected routes require one of:

- `dev-none` mode — no header needed (dev only)
- `internal-only` mode — `X-Auth-Internal-Token: <secret>` header
- `cloudflare-access` mode — `Cf-Access-Jwt-Assertion: <jwt>` header

Public routes: `/health`, `/ready`, `/version` never require auth.