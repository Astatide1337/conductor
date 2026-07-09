# Live E2E — Conductor → Agents Gateway → Skills Gateway

The live end-to-end smoke exercises the real production path through the gateway
infrastructure. It is intentionally **not** part of CI: it makes real HTTP
calls, mutates Conductor state, and creates real tasks on the Agents Gateway.

For the offline mock-based smoke run anytime via
[`scripts/e2e-local.sh`](../scripts/e2e-local.sh).

## Required environment variables

The script refuses to run without these six env vars.

### Conductor itself

| Variable | Purpose | Example |
|---|---|---|
| `CONDUCTOR_BASE_URL` | Where Conductor is listening | `https://conductor.astatide.com` |
| `CONDUCTOR_AUTH_MODE` | Auth model in use by Conductor | `internal-only` |
| `CONDUCTOR_INTERNAL_TOKEN` | Token matching `CONDUCTOR__AUTH__INTERNAL_SECRET` | `s3cret-token` |

Optional — only used in `cloudflare-access` mode:

| Variable | Purpose |
|---|---|
| `CONDUCTOR_CF_ACCESS_JWT` | A valid Cloudflare Access JWT to pass as `Cf-Access-Jwt-Assertion` |

### Agents Gateway

`Conductor` itself must have been started with these as
`CONDUCTOR__AGENTS_GATEWAY__URL` / `CONDUCTOR__AGENTS_GATEWAY__AUTH_MODE` /
`CONDUCTOR__AGENTS_GATEWAY__INTERNAL_TOKEN` env vars (note the double
underscores). The script re-declares them for logging clarity, but the
Conductor server is what makes the call to the gateway.

| Variable | Purpose | Example |
|---|---|---|
| `CONDUCTOR_AGENTS_GATEWAY_URL` | Gateway base URL | `https://agents.astatide.com` |
| `CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE` | Auth the gateway expects from Conductor | `internal-only` |
| `CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN` | Token used by Conductor → Gateway | `gateway-secret` |

### Optional — Skills Gateway

Only required if you want skills validation to actually run. If unset, the
Skills Gateway client is `None` and dispatch will treat
`required_skills=[]`-less tasks as eligible.

| Variable | Purpose | Example |
|---|---|---|
| `CONDUCTOR_SKILLS_GATEWAY_URL` | Skills gateway URL | `https://skills.astatide.com` |
| `CONDUCTOR_SKILLS_GATEWAY_AUTH_MODE` | Auth mode Skills gateway expects | `internal-only` |
| `CONDUCTOR_SKILLS_GATEWAY_INTERNAL_TOKEN` | Token for Conductor → Skills | `skills-secret` |

## How to run

```bash
export CONDUCTOR_BASE_URL=http://localhost:8093
export CONDUCTOR_AUTH_MODE=dev-none
export CONDUCTOR_INTERNAL_TOKEN=ignored-in-dev
export CONDUCTOR_AGENTS_GATEWAY_URL=http://agents.gw.example
export CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE=internal-only
export CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN=...
# optional:
export CONDUCTOR_SKILLS_GATEWAY_URL=http://skills.gw.example
export CONDUCTOR_SKILLS_GATEWAY_AUTH_MODE=internal-only
export CONDUCTOR_SKILLS_GATEWAY_INTERNAL_TOKEN=...

bash scripts/e2e-live-agents.sh
```

## Expected output

A series of section headers (`--- Health ---`, `--- Create Objective ---`,
etc.) ending in:

```
=== Results ===
Passed: 9
Failed: 0
✅ Live E2E passed
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | All checks passed |
| `1` | One or more checks failed |
| `2` | Required env vars were missing |

## What the script checks

1. `/health` reachable and `status == "ok"`
2. `/version` reachable (200)
3. POST `/objectives` returns 201
4. POST `/objectives/{id}/tasks` returns 201
5. POST `/tasks/{id}/dispatch` returns 200
6. GET `/tasks?objective_id=...` reachable and returns a `count`
7. POST `/reconcile` returns 200 with a `{reconciled, transitions, errors, by_target, candidate_count}` payload
8. GET `/events?objective_id=...` reachable and at least 1 event
9. The agent run from step 5 is reported (`status` + `agents_gateway_task_id`)
10. Artifact ingestion is inferred from the event log (`artifacts.ingested` entries)

## Known blockers

- **No live credentials in this environment.** Running without any env vars
  produces:
  ```
  LIVE E2E BLOCKED: missing CONDUCTOR_BASE_URL CONDUCTOR_AUTH_MODE CONDUCTOR_INTERNAL_TOKEN ...
  ```
  and exits 2. This is the documented, intentional behavior.

- The script assumes the production Conductor image is reachable at
  `CONDUCTOR_BASE_URL`. If you want to test against the
  local Docker compose, invoke it as:
  ```bash
  CONDUCTOR_BASE_URL=http://localhost:8093 \
  CONDUCTOR_AUTH_MODE=dev-none \
  CONDUCTOR_INTERNAL_TOKEN=x \
  CONDUCTOR_AGENTS_GATEWAY_URL=http://localhost:8093 \
  CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE=dev-none \
  CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN=x \
  bash scripts/e2e-live-agents.sh
  ```
  Note: in dev-none with a localhost agents gateway URL, Conductor falls
  back to its in-process mock client. The script still exercises the full
  orchestration path through Conductor's own surface; it just doesn't make
  outbound HTTPS calls to a real gateway.

- The script is safe to re-run against the same Conductor — it creates new
  objectives each time and never deletes state.

## Interpreting failures

| Symptom | Diagnostic |
|---|---|
| `health endpoint reachable` FATAL | Conductor is not up at `CONDUCTOR_BASE_URL` |
| `create objective returns 201` fails | Auth header is wrong (check `CONDUCTOR_AUTH_MODE` + token) or body schema drifted |
| `dispatch returns 200` fails | Internal-only cases: dispatch hit a `task.skills_validation_failed` event. Inspect `dispatch.json` for `error` and `missing_skills` |
| `reconcile returns 200` succeeds but `errors > 0` | Gateway call failed during reconcile — check Conductor logs for `reconcile_error` lines |
| `event_count = 0` | Events table is empty. Almost always a storage/config issue |
| `artifact events observed: 0` | Either the gateway produced no artifacts yet, or the gateway-to-Conductor artifact pipeline is misconfigured for `agent_run` -> `agents_gateway_task_id` lookup |

## What it doesn't do

- Doesn't exercise the MCP cockpit surface — use `scripts/e2e-local.sh`
  for that, or write a separate MCP-only smoke (planned for a later
  milestone).
- Doesn't exercise Skills Gateway validation failure paths — those are
  covered in unit-level pytest (`tests/test_orchestrator_flows.py`).
- Doesn't parse the agent_run triumvirate (`agent_run.completed`,
  `agent_run.failed`) for the poll path — it observes them via
  `/reconcile` only. A direct `/agent-runs/{id}` endpoint is conjectured
  for a later milestone.
