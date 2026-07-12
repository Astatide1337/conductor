# Composer LLM

Composer uses an LLM for three decision points:

1. **Specification normalization** — extracting structured requirements from raw text
2. **Planning** — decomposing a spec into an executable task graph
3. **Interaction answering** — responding to agent clarification requests

## Provider abstraction

```python
class ComposerLLMClient:
    async def normalize_spec(raw_spec: str) -> NormalizedSpecResult
    async def create_plan(spec: str, context: str) -> PlanResult
    async def answer_interaction(spec, task, interaction, capture) -> InteractionResult
```

Two implementations:

- **`HttpComposerLLMClient`** — OpenAI-compatible chat/completions API
- **`FakeComposerLLMClient`** — deterministic fake for tests/local E2E

## Response validation

Every LLM response is validated with Pydantic before use:

```python
result = NormalizedSpecResult.model_validate_json(raw_json)
```

Malformed JSON triggers a repair retry: the schema and validation errors are
sent back, and the LLM is asked to produce valid output. After a small number
of retries, persistent errors mark the objective `blocked_external`.

**Unvalidated planner output is never executed.**

## Providers

Configurable via environment variables — any OpenAI-compatible API works:

```bash
CONDUCTOR_COMPOSER_LLM_BASE_URL=https://openrouter.ai/api/v1
CONDUCTOR_COMPOSER_LLM_API_KEY=sk-or-v1-...
CONDUCTOR_COMPOSER_LLM_MODEL=deepseek/deepseek-chat-v3-2
CONDUCTOR_COMPOSER_LLM_TIMEOUT_SECONDS=180
```

Default backend is OpenRouter with `deepseek/deepseek-chat-v3-2`. Not hard-coded.

## When the LLM is called

The LLM is **not** called on every polling tick. It is called only on justified
state transitions:

- Normalizing a specification
- Producing or repairing a plan
- Answering an agent interaction
- Deciding how to restart/redirect a failed agent
- Generating the final human-readable summary

## Security

API keys are never exposed in: logs, task payloads, events, HTML reports, or
MCP responses.