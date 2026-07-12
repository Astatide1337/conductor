# Composer E2E

## Local E2E (deterministic)

```bash
bash scripts/e2e-composer-local.sh
```

Uses `FakeComposerLLMClient` + `MockAgentsGatewayClient` for a deterministic,
offline proof of the full Composer pipeline:

- Spec submitted and normalized
- Plan generated with 2+ parallel implementation tasks + 1 integration task
- Tasks dispatched through mock Agents Gateway
- Reconciliation drives state transitions
- Timeline populated with events
- Objective state visible through API

Expected output: `✅ Composer local E2E passed` with all checks passing.

## Live E2E (real infrastructure)

```bash
bash scripts/e2e-composer-live.sh
```

Requires full gateway infrastructure:

```bash
# Conductor
CONDUCTOR_BASE_URL=http://localhost:8093
CONDUCTOR_AUTH_MODE=internal-only
CONDUCTOR_INTERNAL_TOKEN=...

# Composer LLM
CONDUCTOR_COMPOSER_LLM_BASE_URL=https://openrouter.ai/api/v1
CONDUCTOR_COMPOSER_LLM_API_KEY=sk-or-v1-...
CONDUCTOR_COMPOSER_LLM_MODEL=deepseek/deepseek-chat-v3-2

# Agents Gateway
CONDUCTOR_AGENTS_GATEWAY_URL=http://localhost:8092
CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE=internal-only
CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN=...

# Skills Gateway (required for skill validation)
CONDUCTOR_SKILLS_GATEWAY_URL=http://localhost:8091
CONDUCTOR_SKILLS_GATEWAY_AUTH_MODE=internal-only
CONDUCTOR_SKILLS_GATEWAY_INTERNAL_TOKEN=...

# MCP Gateway (optional, for GitHub tools)
CONDUCTOR_MCP_GATEWAY_URL=http://localhost:8090
```

### Scenario

The live E2E submits a calculator extension spec and waits for:

1. Spec normalization via real Composer LLM
2. Plan generation (at least 2 implementation tasks)
3. Task dispatch through Agents Gateway with real harness sessions
4. Task completion with verification
5. Integration task creation, execution, and completion
6. Full test suite passing
7. Final HTML report generated
8. Objective reaches `completed`

### Timeout

Default: 600 seconds. Override with `COMPOSER_LIVE_TIMEOUT_SEC`.

### Result language

- `COMPOSER LIVE E2E PASSED` — all checks passed
- `COMPOSER LIVE E2E BLOCKED: missing <vars>` — required env missing
- `COMPOSER LIVE E2E TIMED OUT: <stage>` — timed out with exact stage
- `COMPOSER LIVE E2E FAILED: <stage> — <msg>` — failure with exact context

Live success requires: real Conductor, real Agents Gateway, real harness
execution, and real Composer LLM planning.

## Unit/integration tests

```bash
uv run pytest tests/test_composer_e2e.py -q
```

The Python E2E test (`TestComposerE2EFlow.test_spec_to_plan_to_completion`)
drives the complete flow end-to-end using `FakeComposerLLMClient` and
`MockAgentsGatewayClient`, manually simulating task completion and verification
through the mock gateway. This provides deterministic proof of:

```
spec -> plan -> parallel dispatch -> task completion
     -> integration dispatch -> integration completion
     -> verification -> report -> objective completed
```

Also tested: interaction response loop, failed verification prevents
finalization, state durability, and worktree isolation.