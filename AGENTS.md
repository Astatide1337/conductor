# AGENTS.md (Conductor)

This project is operated by AI coding agents. Follow these rules.

## Model policy

- **Only** use `nvidia/nemotron-3-ultra-550b-a55b:free` for LLM-driven
  work in this repo (Composer planner LLM, integration harness, smoke
  tests, etc.). The `:free` suffix is mandatory — it routes to the
  zero-cost OpenRouter tier.
- Do **not** use any other model — not claude, not gpt, not
  moonshotai/kimi, not gemini, not minimax, not deepseek, not anything
  from openrouter beyond `nvidia/nemotron-3-ultra-550b-a55b:free`.
- OpenRouter is the only allowed provider. Other providers
  (Anthropic, OpenAI, NVIDIA direct, etc.) are forbidden — the model
  is accessed **via OpenRouter** only, not via NVIDIA's own API.
- If the `:free` tier returns 429 (rate-limited), retry with backoff.
  Do not fall back to a paid model variant.

## Per-task model passthrough

The Conductor Composer does **NOT** hardcode a model on individual
tasks. Instead the planner LLM is encouraged via the planning prompt
(`conductor/composer/prompts.py`) to fill a per-task `model` field
(`LLMTaskNode.model` / `TaskNode.model` / `IntegrationNode.model`).
When the planner omits the per-task model, the Conductor falls back
to `composer.llm_model` (read from env: `CONDUCTOR_COMPOSER_LLM_MODEL`).

The chosen model is forwarded into every dispatched task spec:

```
scheduler.py / integration.py
  → task_spec["execution"]["model"] = node.model or config.llm_model
  → Agents Gateway reads task_spec.execution.model
  → HarnessDriver injects [profile.model_arg_name, model] into the
    spawn argv (e.g. `pi --model <id>` or `opencode -m <id>`).
```

So the conductor (or, by extension, the planner LLM) chooses which
model each task uses — there is no need to hardcode models anywhere
in AGW's `BUILTIN_PROFILES`. To set a steady default, set
`CONDUCTOR_COMPOSER_LLM_MODEL` in the production env file.

## Harness profile default

- The Conductor's default per-task harness profile is
  `pi-coding-agent` (env: `CONDUCTOR_COMPOSER_DEFAULT_HARNESS_PROFILE`,
  default in `composer.config.ComposerConfig`).
- The integration step also defaults to `pi-coding-agent`
  (env: `CONDUCTOR_COMPOSER_INTEGRATION_HARNESS_PROFILE`).
- The `opencode-deepseek` profile no longer exists in AGW; do not
  reference it. Use `pi-coding-agent` or `opencode` (the latter is
  a configurable opencode profile that takes `-m <model>`).

## Practical settings

- Production compose file: `infra/docker-compose.production.yml`.
- Production env file: `infra/.env.production`. Set at least:
  ```
  CONDUCTOR_COMPOSER_LLM_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
  CONDUCTOR_COMPOSER_LLM_FALLBACK_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
  CONDUCTOR_COMPOSER_DEFAULT_HARNESS_PROFILE=pi-coding-agent
  CONDUCTOR_COMPOSER_INTEGRATION_HARNESS_PROFILE=pi-coding-agent
  OPENROUTER_API_KEY=<key>
  ```
- Conductor runs as a Docker container (`infra-conductor-1`). Health
  check: `curl -sf -H "X-Auth-Internal-Token: $TOK"
  http://localhost:8093/health` — `composer_llm_model` field shows
  the active planner model.
- The conductor SQLite DB lives at `conductor/data/conductor.db`.
- Live E2E script: `conductor/scripts/e2e-composer-live.sh`.
- Do **not** use `deepseek/deepseek-v4-flash` (paid) or any OpenAI /
  Anthropic / Gemini / MiniMax model as a "fallback" — the OpenRouter
  account has $0 credit and we are testing with free-tier only.

### Live-dev with the bind mount — IMPORTANT

The production compose file bind-mounts the host source tree into the
container at `/app/conductor:ro` so source edits take effect without
rebuilding the image.  However, the image ships a **pip-installed**
copy of the conductor package in
`/usr/local/lib/python3.12/site-packages/conductor`.  When the
container's entrypoint (`/usr/local/bin/conductor run`) launches Python,
`sys.path[0]` is set to the script directory (`/usr/local/bin`),
**not** the bind-mount — so Python discovers the installed package at
`/usr/local/lib/python3.12/site-packages/conductor` instead of the
bind-mount at `/app/conductor`.  Edits to the host source tree would
silently be ignored.

The fix (run once per container, or bake into the image later):

```bash
docker exec --user root -i infra-conductor-1 \
  pip uninstall -y astatide-conductor
docker exec --user root -i infra-conductor-1 \
  pip install --no-deps -e /app
```

This installs the package as **editable** pointing at the
bind-mount.  After this, any edit to `/home/ubuntu/conductor/conductor/**`
is visible to the running server (a simple `docker restart
infra-conductor-1` suffices after edits — clear `__pycache__` first to
avoid stale compile cache).

If you change the bind-mount location or remove it altogether, this
editable install is no longer needed. The Dockerfile does not bake
`pip install -e .` because the bind-mount is a live-dev convenience,
not a production code path; production deploys bake the package into
the image via `pip install .` at build time (see `Dockerfile`) and do
not bind-mount host source.

## Repo-specific pointers

- Composer core: `conductor/composer/{service,planner,scheduler,
  integration,interactions,goals,context,prompts,models,llm,
  storage,events,reports}.py`.
- Config schema: `conductor/config.py` (`ComposerConfig`).
- The `TaskNode.model`, `IntegrationNode.model`, `LLMTaskNode.model`
  fields are deliberately separate from `harness_profile` — the
  harness profile says *which CLI* to launch; the model says *which
  LLM that CLI uses*.
- AGW integration client: `conductor/clients/agents_gateway.py`.
- Service entrypoint: `conductor/server.py`.
- The planner LLM is configured in `conductor/server.py:219` based
  on `composer.llm_api_key + composer.llm_model`. If neither is set,
  a `FakeComposerLLMClient` is used (smoke path).

## Health gate before any LLM-driven work

```bash
curl -sf -H "X-Auth-Internal-Token: $CONDUCTOR_INTERNAL_TOKEN" \
  http://localhost:8093/health
curl -sf -H "X-Auth-Internal-Token: $TOK" \
  http://localhost:8092/harness-profiles/pi-coding-agent/availability
```

If either fails, do not dispatch more tasks; surface in the report.

## Scaling knobs that prevent 402s

- The `:free` tier has stricter rate limits — keep
  `composer.llm_max_tokens` at its default (2048).
- Do not dispatch more than `composer.max_parallel_tasks` (default 3)
  simultaneously — the `:free` tier rejects bursty concurrency.
- PI harness should always be launched with `--thinking off` (which
  is hard-coded in the `pi-coding-agent` profile's `args`). Do not
  escalate to medium/high/xhigh thinking.

## What this repo is not

- This project does **not** ship paid-model fallbacks. If a PR adds
  `claude-sonnet` / `gpt-4o` / `deepseek-v4-flash` fallback paths,
  reject the PR.
- The deleted AGW profile `opencode-deepseek` was the historical
  source of silent profile-substitution bugs (a hard-coded paid
  model that fell back to itself when the dispatcher omitted
  `harness_profile`). The fix is structural — the planner's
  `harness_profile` default is now `pi-coding-agent`, the storage
  layer's column default is `pi-coding-agent`, and the AGW default
  profile is `pi-coding-agent`. Do not reintroduce hardcoded paid
  model profiles.
