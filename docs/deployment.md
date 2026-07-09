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