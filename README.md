# Astatide Conductor

Persistent objective/task-graph orchestrator for MCP-driven agent workflows.

> The Conductor is a durable coordination layer between MCP-capable cockpits and the existing gateway substrate.

## What Conductor is

- A persistent objective orchestrator
- A task graph manager with durable state
- An approval and policy enforcement layer
- A dispatch coordinator to Agents Gateway
- A high-level MCP cockpit surface

## What Conductor is NOT

- NOT a shell runner
- NOT a coding agent (Claude, ChatGPT, opencode, Codex)
- NOT a replacement for Agents Gateway
- NOT a direct tmux/session manager
- Does NOT bypass human approval for irreversible actions

## Architecture

```
Any MCP-compatible cockpit (Claude / ChatGPT / Polychat / CLI / future UI)
       |
       v
   CONDUCTOR  ─── single hub (REST + MCP)
       |
   +---+---+---+---+---------+
   |   |   |   |   |         |
   v   v   v   v   v         v
Agents Skills MCP wiki  future gateways
Gateway Gateway Gateway mcp  (mail / calendar / cloud / deploy)
```

Conductor is the single hub. Gateways are downstream capability providers
— the MCP Gateway is one of them, not the parent of Conductor. See
[`docs/gateway-hub.md`](docs/gateway-hub.md) for the full Gateway Hub
reference.

## Auth model

Three modes (`CONDUCTOR_AUTH__MODE`):

| Mode | Behavior |
|---|---|
| `dev-none` | No auth (dev/local only). Refuses to boot if `CONDUCTOR_ENVIRONMENT=production`. |
| `internal-only` | Requires `X-Auth-Internal-Token` matching `CONDUCTOR_AUTH__INTERNAL_SECRET`. |
| `cloudflare-access` | Cloudflare Access JWT (`Cf-Access-Jwt-Assertion`) or internal token. |

The MCP cockpit surface at `/mcp` is **auth-checked by the same middleware**
as the REST API. Unauthenticated MCP traffic gets a `401` with a JSON-RPC 2.0
error envelope (code `-32001`), so cockpits can parse the rejection cleanly:

```json
{"jsonrpc":"2.0","error":{"code":-32001,"message":"..."},"id":null}
```

REST endpoints keep their normal FastAPI `{"detail": "..."}` shape — those
are **not** JSON-RPC envelopes.

## Skill validation before dispatch

A task that declares `required_skills` is validated against the Skills
Gateway **before any state transition** in `dispatch_task`:

1. `created → (validate skills) → ready → dispatched → running`
2. If validation fails: `task.skills_validation_failed` event is emitted with
   the missing skills in the payload, the task is left in its original
   state, **no** `agent_run` row is created, **no** Agents Gateway call is
   made, and the caller receives a structured error including `missing_skills`.
3. If the gateway is not configured (`CONDUCTOR_SKILLS_GATEWAY_URL` unset or
   localhost), validation is a no-op and the task dispatches normally.

## Capability validation before dispatch

A task may declare `required_capabilities` in its `metadata` (a JSON list
of dotted capability strings like `["execution.task.create", "external.github"]`).
Conductor's Gateway Hub validates each capability has at least one
configured+enabled provider **before** any state transition. Missing or
degraded capabilities block dispatch and emit a
`task.capabilities_validation_failed` event; passing emits
`task.capabilities_validated`. See [`docs/gateway-hub.md`](docs/gateway-hub.md).

## Local dev

```bash
uv sync
uv run conductor --help
uv run conductor run --port 8093
```

## Config

```bash
cp .env.example .env
# Edit .env as needed
```

Config precedence: CLI flags > env vars > YAML > defaults.

## Testing

```bash
uv run pytest -q                       # 390+ tests, all offline (uses mocks)
bash scripts/e2e-local.sh              # 15/15 smoke (offline, pre-existing)
bash scripts/e2e-local-gateway-hub.sh  # 17/17 gateway hub smoke (offline)
```

## Docker

```bash
docker compose config
docker compose up -d --build
```

## Live E2E (real gateway)

Two live E2E pathways, both refusing to run without credentials:

| Script | Scope |
|---|---|
| `scripts/e2e-live-agents.sh` | Agents Gateway + Skills Gateway |
| `scripts/e2e-live-gateway-hub.sh` | Agents + Skills + MCP Gateway (+ optional wiki) |

See [`docs/live-e2e.md`](docs/live-e2e.md) for the env var list, expected
output, and interpretation guide.

## Deployment modes

### Edge-auth-only personal mode

Cloudflare Access protects hostname. App uses `dev-none` or `internal-only`.

### Defense-in-depth production mode

Cloudflare Access + app validates Cloudflare Access JWTs.

## Known limitations

- LLM planner is **not** built; deferred to a later milestone.
- Autonomous loop mode is **not** built; deferred.
- Harness/tmux runtime is **not** built; only the existing Mock/HTTP
  Agents Gateway clients are exercised.
- Live E2E only runs when real gateway credentials are provided. The
  scripts exit 2 and list the missing env vars otherwise.
- Skills validation requires Skills Gateway configuration.
- The MCP Gateway downstream client supports `health / version /
  tools/list / tools/call` — a standard MCP surface. Custom gateway
  deployments with a different surface may need a new client adapter.
- Capability catalog is static per gateway kind for this milestone.
  Dynamic discovery is planned for a later milestone.
- Conductor does NOT run shell commands, NOT bypass Agents Gateway, NOT
  bypass human approval for irreversible actions.