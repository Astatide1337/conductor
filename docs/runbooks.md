# Runbooks

## Common operations

### Create and dispatch a task

```bash
# Create objective
OBJ=$(curl -s -X POST localhost:8093/objectives \
  -H "Content-Type: application/json" \
  -d '{"title":"Fix auth bug","priority":"high"}')
OBJ_ID=$(echo $OBJ | jq -r .objective_id)

# Create task
TASK=$(curl -s -X POST localhost:8093/objectives/$OBJ_ID/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"Debug token refresh","task_type":"scout"}')
TASK_ID=$(echo $TASK | jq -r .id)

# Dispatch to Agents Gateway
curl -s -X POST localhost:8093/tasks/$TASK_ID/dispatch
```

### Check status

```bash
curl -s localhost:8093/objectives/$OBJ_ID | jq .
```

### Approve an action

```bash
# List pending approvals
curl -s localhost:8093/approvals | jq .

# Approve by ID
curl -s -X POST localhost:8093/approvals/$APPROVAL_ID/approve | jq .
```

### Run a dry run

```bash
curl -s -X POST localhost:8093/dry-run | jq .
```

### View events

```bash
curl -s "localhost:8093/events?objective_id=$OBJ_ID&limit=20" | jq .
```

### View full timeline

```bash
curl -s "localhost:8093/objectives/$OBJ_ID/timeline" | jq .
```

The timeline returns events in **chronological order** (oldest first) and
includes everything that happened with the objective: `objective.created`,
`task.created`, gateway checks, dispatches, reconciliations, approvals.
Cockpits use this as the "What happened with objective X?" entry point.

### List gateways + capabilities

```bash
curl -s localhost:8093/gateways | jq .
curl -s localhost:8093/gateways/status | jq .
# Live health probe
curl -s -X POST localhost:8093/gateways/check-all | jq .
# Capability search
curl -s localhost:8093/capabilities | jq .
curl -s localhost:8093/capabilities/execution.task.create | jq .
```

## Startup

```bash
# Local dev
uv sync
uv run conductor run --port 8093

# Docker
docker compose up -d --build
```

Verify:
```bash
curl localhost:8093/health  # {"status":"ok"}
curl localhost:8093/ready   # {"ready":true,...}
curl localhost:8093/version # {"service":"astatide-conductor",...}
```

## Health checks

| Check | Command | Expected |
|---|---|---|
| Liveness | `curl /health` | 200, `{"status":"ok"}` |
| Readiness | `curl /ready` | 200, `checks.storage == "ok"` |
| Version | `curl /version` | 200, service + version |

## Troubleshooting

### Objective stuck at "created"

```bash
# Must transition to active before pause/resume
curl -s -X POST localhost:8093/objectives/$ID/resume | jq .
```

Now it's `active`. Then you can pause, resume, or cancel.

### "Invalid transition" error

State machines enforce transitions. Check the status first:

```bash
curl localhost:8093/objectives/$ID | jq '.objective.status'
```

Valid paths:
- created → active → paused/blocked/completed/failed/cancelled
- paused → active
- blocked → active/failed

### Circuit breaker tripped

Check dry-run output:
```bash
curl -s -X POST localhost:8093/dry-run | jq .
```

If `approval_gates` shows breaker trips, you need to:
- Manually adjust limits via env/config
- Or approve the escalation

### Dispatch idempotency

Same idempotency key `obj_id:run_id:task_id:attempt` will not create
duplicate agent gateway tasks. Agent runs are tracked in DB.

## Database inspection

```bash
sqlite3 data/conductor.db ".tables"
sqlite3 data/conductor.db "SELECT id,title,status FROM objectives;"
sqlite3 data/conductor.db "SELECT id,status FROM tasks WHERE run_id='...';"
sqlite3 data/conductor.db "SELECT event_type,message FROM events ORDER BY created_at DESC LIMIT 10;"
```

## Docker operations

```bash
# Build and start
docker compose up -d --build

# View logs
docker compose logs -f

# Stop
docker compose down

# Remove data (fresh start)
rm -rf data/
docker compose up -d --build
```

## Resetting state

The database is at `CONDUCTOR_STORAGE__SQLITE_PATH` (default `./data/conductor.db`).
Delete it for a completely fresh state:

```bash
rm -f data/conductor.db
```

## Reconciliation after restart

Conductor is durable: state lives in SQLite. After a crash/restart, agent
runs left in `dispatched` / `queued` / `running` / `lost` need to be polled
against the Agents Gateway to discover terminal state.

```bash
curl -s -X POST localhost:8093/reconcile | jq .
```

Response:
```json
{
  "reconciled": 4,
  "transitions": 2,
  "errors": 0,
  "by_target": {"completed": 1, "failed": 1},
  "candidate_count": 4
}
```

- `transitions > 0` means async work happened on the gateway side while
  Conductor was down — good, that's what the recovery is for.
- `errors > 0` means the gateway was unreachable or returned a malformed
  response — inspect Conductor logs for `reconcile_error` lines.

Safe to call repeatedly. Reconcile also ingests any artifacts the gateway
has produced, idempotently (artifact de-dup by id).

## Dispatch with skills validation

```bash
# Create a task declaring required skills
TASK=$(curl -s -X POST localhost:8093/objectives/$OBJ_ID/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"Build auth","required_skills":["code-review","git-tools"]}')
TASK_ID=$(echo $TASK | jq -r .id)

# Dispatch — validates skills first
RESP=$(curl -s -X POST localhost:8093/tasks/$TASK_ID/dispatch)
echo "$RESP" | jq .
```

When skills are missing, `$RESP` looks like:
```json
{
  "task_id": "...",
  "status": "ready",        // unchanged
  "agent_run": null,        // no row created
  "error": "missing required skills: ['code-review']",
  "missing_skills": ["code-review"]
}
```

The task remains in its original state. Re-dispatch after registering the
missing skill with the Skills Gateway, or retag the task with skills that
exist.

### Inspecting the failure event

```bash
curl -s "localhost:8093/events?task_id=$TASK_ID" | \
  jq '.events[] | select(.event_type=="task.skills_validation_failed")'
```

The event payload includes `original_status`, `validated_skills`, and
`missing_skills` for forensic inspection.

## Dispatch with capability validation

A task may declare `required_capabilities` in its `metadata` (a JSON list of
dotted capability strings). Conductor's Gateway Hub validates each
required capability has at least one configured+enabled provider **before**
any state transition.

```bash
# Create a task declaring required capabilities
TASK=$(curl -s -X POST localhost:8093/objectives/$OBJ_ID/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title":"Bridge auth audit",
    "required_skills":["code-review"],
    "metadata":{"required_capabilities":["execution.task.create","external.github"]}
  }')
TASK_ID=$(echo $TASK | jq -r .id)

# Dispatch — validates capabilities first
RESP=$(curl -s -X POST localhost:8093/tasks/$TASK_ID/dispatch)
echo "$RESP" | jq .
```

When capabilities are missing, `$RESP` looks like:
```json
{
  "task_id": "...",
  "status": "ready",
  "agent_run": null,
  "error": "missing required capabilities: ['external.github']",
  "missing_capabilities": ["external.github"],
  "degraded_capabilities": [],
  "satisfied_capabilities": ["execution.task.create"]
}
```

To inspect the failure:

```bash
curl -s "localhost:8093/events?task_id=$TASK_ID" | \
  jq '.events[] | select(.event_type=="task.capabilities_validation_failed")'
```

To find candidate gateways for a capability:

```bash
curl -s localhost:8093/capabilities/external.github | jq .
```

To enable a missing capability, configure the corresponding gateway env
var (e.g. `CONDUCTOR_MCP_GATEWAY__URL` to expose the `mcp` gateway
which provides `external.github`).

## Gateway hub health

```bash
# Lightweight snapshot — no live probes
curl -s localhost:8093/gateways/status | jq .

# Single live probe
curl -s -X POST localhost:8093/gateways/agents/check | jq .

# Probe everything
curl -s -X POST localhost:8093/gateways/check-all | jq .
```

A `gateway.health_checked` (status=healthy) or `gateway.health_failed`
(any other status) event is emitted on every user-triggered probe.

Per-status interpretation:

| Status | Meaning |
|---|---|
| `not_configured` | `base_url` is empty — set `CONDUCTOR_*__URL` env vars |
| `disabled` | Gateway `enabled=False` — flip via config to enable |
| `healthy` | `/health` returned 2xx |
| `auth_failed` | `/health` returned 401 or 403 — token mismatch |
| `timeout` | `/health` did not respond within `timeout_seconds` |
| `unhealthy` | 5xx or other 4xx — server error or refused |
| `error` | Unexpected client-side exception — inspect Conductor logs |

## MCP cockpit unauthorized

If your MCP cockpit gets a `401` with this body:

```json
{"jsonrpc":"2.0","error":{"code":-32001,"message":"missing internal token"},"id":null}
```

it means the cockpit is missing the auth header. This is the same check the
REST API performs; there is no separate MCP auth bypass path.

| Mode | Required header |
|---|---|
| `dev-none` | none (dev only) |
| `internal-only` | `X-Auth-Internal-Token: <CONDUCTOR_AUTH__INTERNAL_SECRET>` |
| `cloudflare-access` | `Cf-Access-Jwt-Assertion: <jwt>` (or the internal token bypass) |

The body shape (`jsonrpc` + `error.code == -32001`) is intentional so MCP
clients can parse the error. REST endpoints will return `{"detail": "..."}` —
that's the expected REST shape and is not a bug.

## Live E2E (production gateway)

Two live E2E pathways:

| Script | Scope |
|---|---|
| `scripts/e2e-live-agents.sh` | Agents + Skills Gateway |
| `scripts/e2e-live-gateway-hub.sh` | Agents + Skills + MCP Gateway + optional wiki |

Quick recipe for the full Gateway Hub live smoke:

```bash
export CONDUCTOR_BASE_URL=http://conductor.astatide.com
export CONDUCTOR_AUTH_MODE=internal-only
export CONDUCTOR_INTERNAL_TOKEN=...
export CONDUCTOR_AGENTS_GATEWAY_URL=http://agents.astatide.com
export CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE=internal-only
export CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN=...
export CONDUCTOR_SKILLS_GATEWAY_URL=http://skills.astatide.com
export CONDUCTOR_SKILLS_GATEWAY_AUTH_MODE=internal-only
export CONDUCTOR_SKILLS_GATEWAY_INTERNAL_TOKEN=...
export CONDUCTOR_MCP_GATEWAY_URL=http://mcp.astatide.com
export CONDUCTOR_MCP_GATEWAY_AUTH_MODE=internal-only
export CONDUCTOR_MCP_GATEWAY_INTERNAL_TOKEN=...
# optional:
export CONDUCTOR_WIKI_MCP_URL=http://wiki.astatide.com
export CONDUCTOR_WIKI_MCP_AUTH_MODE=internal-only
export CONDUCTOR_WIKI_MCP_INTERNAL_TOKEN=...
bash scripts/e2e-live-gateway-hub.sh
```

If env vars are missing, the script prints a clear blocker message and
exits 2 — it will never fake a live run.