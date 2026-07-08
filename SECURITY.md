# Security

## Authentication modes

| Mode | Description | When to use |
|---|---|---|
| `dev-none` | No authentication | Local development only. Refused in production. |
| `internal-only` | Shared secret via `X-Auth-Internal-Token` header | Personal deployments behind Cloudflare Access |
| `cloudflare-access` | Cloudflare Access JWT verification | Multi-user production deployments |

## Deployment modes

### Edge-auth-only personal mode
Cloudflare Access protects the hostname. App uses `dev-none` or `internal-only`.
Origin is private. Suitable for personal MCP usage behind CF Access.

### Defense-in-depth production mode
Cloudflare Access protects the hostname.
App also validates Cloudflare Access JWTs (JWT signature, audience, issuer, expiry).
Suitable for multi-user or stronger production posture.

## Irreversible action policy

The Conductor enforces explicit approval gates for irreversible actions:

- `merge_main` — requires approval
- `deploy_production` — requires approval
- `modify_secrets` — requires approval
- `modify_cloudflare` — requires approval
- `delete_data` — requires approval
- `spend_money` — requires approval
- `destructive_command` — requires approval
- `production_db_migration` — requires approval

Autonomous actions (allowed without approval):
- `create_objective`, `create_task`, `dispatch_task`, `run_tests`,
  `collect_artifacts`, `summarize_results`, `mark_complete`, `mark_blocked`,
  `dry_run`

## Agent output is untrusted

The Conductor treats all agent output as untrusted data. Raw agent text is never
used as direct instructions. Unsafe patterns in agent output (e.g. "deploy
production now", "rm -rf") are flagged and escalate to the approval queue.

## Circuit breakers

Hard safety limits prevent runaway loops:

| Breaker | Default | Behavior when tripped |
|---|---|---|
| `max_iterations_per_run` | 50 | Pause/block objective |
| `max_cost_usd_per_run` | $10.00 | Block dispatch |
| `max_concurrent_tasks` | 4 | Prevent dispatch |
| `max_retries_per_task` | 3 | Block retry |
| `max_wall_clock_minutes` | 120 | Pause objective |
| `max_stall_minutes` | 30 | Pause objective |

## What Conductor does NOT do

- Does NOT run shell commands directly
- Does NOT create tmux sessions
- Does NOT bypass Agents Gateway
- Does NOT bypass human approval for irreversible actions
- Does NOT treat agent output as authority

## Session security

Protected routes: `/objectives`, `/tasks`, `/approvals`, `/events`,
`/reconcile`, `/dry-run`, `/mcp`, `/metrics`

Public routes: `/health`, `/ready`, `/version`

## Log redaction

Sensitive headers are redacted from logs:
- `Authorization`, `Cookie`, `Cf-Access-Jwt-Assertion`
- `X-Auth-Internal-Token`, `X-Conductor-Internal-Token`

## Production boot assertions

- `dev-none` auth mode refused in `production` environment
- Internal-only mode requires non-empty secret
- Cloudflare-access mode requires team domain configured