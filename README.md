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
Any MCP-capable cockpit
      |
      v
MCP Gateway ──────────────────────┐
      |                            │
      v                            v
Conductor ◄───────────────── Skills Gateway + wiki-mcp
      |
      v
Agents Gateway
      |
      v
Runtime substrate (process/docker/future harness-tmux)
```

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
uv run pytest -q        # 289+ tests, all offline (uses mocks)
bash scripts/e2e-local.sh   # 15/15 smoke (offline)
```

## Docker

```bash
docker compose config
docker compose up -d --build
```

## Live E2E (real gateway)

The full path through the production Agents Gateway + Skills Gateway is
exercised by `scripts/e2e-live-agents.sh`. It refuses to run without
credentials. See [`docs/live-e2e.md`](docs/live-e2e.md) for the env var
list, expected output, and interpretation guide.

```bash
export CONDUCTOR_BASE_URL=...
export CONDUCTOR_AUTH_MODE=internal-only
export CONDUCTOR_INTERNAL_TOKEN=...
export CONDUCTOR_AGENTS_GATEWAY_URL=...
export CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE=internal-only
export CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN=...
bash scripts/e2e-live-agents.sh
```

## Deployment modes

### Edge-auth-only personal mode

Cloudflare Access protects hostname. App uses `dev-none` or `internal-only`.

### Defense-in-depth production mode

Cloudflare Access + app validates Cloudflare Access JWTs.

## Known limitations

- LLM planner is **not** built; deferred to a later milestone.
- Harness/tmux runtime is **not** built; only the existing Mock/HTTP
  Agents Gateway clients are exercised.
- Live E2E only runs when real gateway credentials are provided. The script
  exits 2 and lists the missing env vars otherwise.
- Skills validation requires Skills Gateway configuration.
- Conductor does NOT run shell commands, NOT bypass Agents Gateway, NOT
  bypass human approval for irreversible actions.