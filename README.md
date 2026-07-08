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
uv run pytest -q
```

## Docker

```bash
docker compose config
docker compose up -d --build
```

## Deployment modes

### Edge-auth-only personal mode

Cloudflare Access protects hostname. App uses `dev-none` or `internal-only`.

### Defense-in-depth production mode

Cloudflare Access + app validates Cloudflare Access JWTs.

## Known limitations

- LLM planner is optional and not default
- No autonomous irreversible actions
- Requires Agents Gateway for task execution
- Skills validation requires Skills Gateway