# Deployment

## Configuration

Config precedence: CLI flags > environment variables > YAML config file > defaults.

### Environment variables

All prefixed with `CONDUCTOR_`, double-underscore nesting:

```bash
CONDUCTOR_SERVICE__PORT=8093
CONDUCTOR_AUTH__MODE=internal-only
CONDUCTOR_AUTH__INTERNAL_SECRET=your-secret
CONDUCTOR_OBSERVABILITY__LOG_LEVEL=INFO
CONDUCTOR_OBSERVABILITY__LOG_FORMAT=json
CONDUCTOR_ENVIRONMENT=production
```

### YAML config

Default file: `conductor.yaml`. Override with `CONDUCTOR_CONFIG` env var.

### CLI flags

```bash
conductor run --host 0.0.0.0 --port 8093 --config /etc/conductor.yaml
```

## Deployment modes

### Mode 1: Edge-auth-only personal mode

```
Internet → Cloudflare Access → Conductor (dev-none or internal-only)
```

- Cloudflare Access protects the hostname
- Origin server is private
- App uses `dev-none` (local) or `internal-only` (personal deployment)
- Port mapped to `127.0.0.1` in docker-compose

```yaml
# docker-compose overrides
environment:
  CONDUCTOR_AUTH__MODE: internal-only
  CONDUCTOR_AUTH__INTERNAL_SECRET: ${CONDUCTOR_INTERNAL_SECRET}
  CONDUCTOR_ENVIRONMENT: production
```

### Mode 2: Defense-in-depth production mode

```
Internet → Cloudflare Access → Conductor (cloudflare-access JWT validation)
```

- Cloudflare Access protects the hostname
- App also validates Cloudflare Access JWTs
- RS256 signature verification
- Audience and issuer validation
- Suitable for multi-user deployments

Required env:
```bash
CONDUCTOR_AUTH__MODE=cloudflare-access
CONDUCTOR_AUTH__CLOUDFLARE_TEAM_DOMAIN=<team>.cloudflareaccess.com
CONDUCTOR_AUTH__CLOUDFLARE_AUD=<audience-tag>
```

## Docker

### Quick start

```bash
cp .env.example .env
# Edit secrets as needed
docker compose up -d --build
curl http://localhost:8093/health
```

### Production docker compose

```yaml
services:
  conductor:
    build: .
    ports:
      - "127.0.0.1:8093:8093"
    volumes:
      - ./data:/data
      - ./conductor.yaml:/app/conductor.yaml:ro
    env_file:
      - .env
    environment:
      CONDUCTOR_AUTH__MODE: ${AUTH_MODE:-cloudflare-access}
      CONDUCTOR_ENVIRONMENT: production
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8093/health').read()"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 5s
```

### Volume mounts

| Mount | Purpose |
|---|---|
| `./data:/data` | SQLite database persistence |
| `./conductor.yaml:/app/conductor.yaml:ro` | Config file (read-only) |

## Port allocations

| Service | Port |
|---|---|
| Skills Gateway | 8091 |
| Agents Gateway | 8092 |
| **Conductor** | **8093** |
| MCP Gateway | 8080 |
| wiki-mcp | 8081 |

## Reverse proxy (optional)

Conductor behind nginx/Caddy at e.g. `https://conductor.astatide.com`:

```nginx
location / {
    proxy_pass http://127.0.0.1:8093;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Protected routes: `/objectives`, `/tasks`, `/approvals`, `/events`,
`/reconcile`, `/dry-run`, `/mcp`, `/metrics`.

All path-based auth handled by Conductor's auth middleware.

## Integration config

### Agents Gateway

```bash
CONDUCTOR_AGENTS_GATEWAY__URL=http://agents-gateway:8092
CONDUCTOR_AGENTS_GATEWAY__AUTH_MODE=internal-only
CONDUCTOR_AGENTS_GATEWAY__INTERNAL_TOKEN=<shared-secret>
CONDUCTOR_AGENTS_GATEWAY__TIMEOUT_SECONDS=30
```

### Skills Gateway

```bash
CONDUCTOR_SKILLS_GATEWAY__URL=http://skills-gateway:8091
CONDUCTOR_SKILLS_GATEWAY__AUTH_MODE=dev-none
CONDUCTOR_SKILLS_GATEWAY__TIMEOUT_SECONDS=10
```

## Auth + MCP

The mounted `/mcp` sub-app is protected by the **same** auth middleware as
the REST API. There is no special-cased auth path for MCP.

Behavior on failure differs by surface so MCP cockpits can parse the
rejection:

| Surface | 401 body shape |
|---|---|
| REST (`/objectives`, `/tasks`, ...) | `{"detail": "..."}` (FastAPI default) |
| MCP (`/mcp`, `/mcp/*`) | `{"jsonrpc":"2.0","error":{"code":-32001,"message":"..."},"id":null}` |

Both cases are HTTP `401`. The MCP shape is intentional — JSON-RPC 2.0
clients cannot make sense of the FastAPI `{"detail":...}` envelope.

In `dev-none` mode, neither surface enforces auth (dev only).
In `internal-only` mode, both require `X-Auth-Internal-Token`.
In `cloudflare-access` mode, both require either a Cloudflare Access JWT
or an internal bypass token.

## Skill validation before dispatch

`POST /tasks/{id}/dispatch` validates `required_skills` against the
configured Skills Gateway **before** any state transition or gateway call.
If skills are missing, dispatch fails fast: no `agent_run` is created, the
Agents Gateway is not contacted, and the task remains in its original
state. See `docs/api.md#dispatch--skill-validation-gate` for the response
shape.

Required env to enable skill validation in production:

```bash
CONDUCTOR_SKILLS_GATEWAY__URL=http://skills-gateway:8091   # non-localhost URL
CONDUCTOR_SKILLS_GATEWAY__AUTH_MODE=internal-only
CONDUCTOR_SKILLS_GATEWAY__INTERNAL_TOKEN=<secret>
```

If unset, the Skills Gateway client is `None` and `required_skills` are
silently skipped. This is acceptable for personal dev deployments but
**not** for production.

## Live E2E against real gateways

The production smoke `scripts/e2e-live-agents.sh` exercises the full
Conductor → Agents Gateway → Skills Gateway path. See
[`docs/live-e2e.md`](live-e2e.md) for the env var checklist and
expected output.

```bash
export CONDUCTOR_BASE_URL=http://conductor.astatide.com
export CONDUCTOR_AUTH_MODE=internal-only
export CONDUCTOR_INTERNAL_TOKEN=...
export CONDUCTOR_AGENTS_GATEWAY_URL=http://agents.astatide.com
export CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE=internal-only
export CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN=...
bash scripts/e2e-live-agents.sh
```

Exit codes: `0` success, `1` assertion failure, `2` missing required env vars.

## Production boot assertions

The application refuses to start with unsafe configs:

1. `dev-none` auth mode refused in `production` environment
2. `internal-only` mode requires non-empty internal secret
3. Raise `RuntimeError` immediately at boot

## Circuit breaker defaults

| Parameter | Default | Env var |
|---|---|---|
| max_iterations_per_run | 50 | `CONDUCTOR_CIRCUIT__MAX_ITERATIONS_PER_RUN` |
| max_cost_usd_per_run | 10.0 | `CONDUCTOR_CIRCUIT__MAX_COST_USD_PER_RUN` |
| max_concurrent_tasks | 4 | `CONDUCTOR_CIRCUIT__MAX_CONCURRENT_TASKS` |
| max_retries_per_task | 3 | `CONDUCTOR_CIRCUIT__MAX_RETRIES_PER_TASK` |
| max_wall_clock_minutes | 120 | `CONDUCTOR_CIRCUIT__MAX_WALL_CLOCK_MINUTES` |
| max_stall_minutes | 30 | `CONDUCTOR_CIRCUIT__MAX_STALL_MINUTES` |

Increase for specific campaigns by passing custom `max_iterations` / `max_cost_usd` when creating runs.