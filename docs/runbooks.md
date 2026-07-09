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