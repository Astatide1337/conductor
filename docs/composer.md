# Composer v1 — Spec-to-Verified-Execution Engine

Composer is the planning and supervision engine inside **Conductor**. It is not
a separate service — Conductor remains the single external MCP/API surface.

## Architecture

```
Any MCP-compatible cockpit
        |
        v
Conductor (single external surface)
        |
        +--> Composer (planning + supervision)
        |        |
        |        +--> normalize spec
        |        +--> gather context
        |        +--> create executable task graph
        |        +--> assign harnesses, skills, verification
        |        +--> dispatch ready tasks in parallel
        |        +--> answer agent interactions
        |        +--> integrate completed branches
        |        +--> verify completion
        |        +--> produce HTML/JSON reports
        |
        +--> Agents Gateway (worktrees + harness sessions)
        +--> Skills Gateway (methodology and execution skills)
        +--> MCP Gateway (GitHub and external tools)
        +--> wiki-mcp (durable project context)
```

## Workflow

1. Cockpit refines and submits a finalized specification to Conductor.
2. Composer normalizes the spec via LLM.
3. Composer gathers project and gateway context.
4. Composer creates an executable task graph (DAG).
5. Composer dispatches independent tasks in parallel through Agents Gateway.
6. Agents work in isolated worktrees with assigned harness profiles.
7. Composer monitors progress, verification, and interactions.
8. Composer answers agent questions automatically (no human queue).
9. All tasks complete → Composer dispatches an integration task.
10. Integration combines branches, runs full verification.
11. Composer produces HTML + JSON review reports.
12. Objective reaches `completed`.

## Human interaction model

**Review-after-execution** — not approve before each action.

No human approval required for: planning, task decomposition, worktree creation,
file edits, test runs, skill selection, commits, integration, or reporting.

## Supported harness profiles

Composer selects the best harness profile for each task based on capability and
availability. Currently supported:

- **opencode-deepseek** — primary harness (OpenCode + DeepSeek)
- **pi-coding-agent** — Pi minimal agent harness (Earendil)
- **claude-code** — Claude Code (Anthropic)

Additional harnesses can be registered through Agents Gateway.

## LLM providers

Composer supports any OpenAI-compatible chat API. OpenRouter is the default
backend but not hard-coded.

Provider abstraction: `ComposerLLMClient`
- `HttpComposerLLMClient` — real HTTP calls with JSON validation + repair
- `FakeComposerLLMClient` — deterministic for tests

Every LLM response is validated with Pydantic. Invalid JSON triggers a repair
retry. Persistent provider failures mark the objective `blocked_external`.

## State model

| Entity | Statuses | Storage |
|--------|----------|---------|
| Composer spec | received, normalizing, normalized, planning, planned, executing, integrating, verifying, completed, blocked_external, failed, cancelled | `composer_specs` |
| Composer plan | draft, validated, active, integrating, completed, blocked_external, failed, superseded | `composer_plans` |
| Plan task | pending, ready, dispatching, running, waiting_for_reply, verifying, completed, blocked_external, failed, cancelled | `composer_plan_tasks` |
| Interaction decision | (reply, redirect, restart_task, mark_external_blocker) | `composer_interaction_decisions` |
| Report | completed | `composer_reports` |

All tables live in the same SQLite database as Conductor. Restart survival is
tested.

## HTTP API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/composer/objectives` | Submit finalized spec |
| GET | `/composer/objectives` | List composer objectives |
| GET | `/composer/objectives/{id}` | Get full objective state |
| GET | `/composer/objectives/{id}/spec` | Get composer spec |
| GET | `/composer/objectives/{id}/plan` | Get execution plan |
| GET | `/composer/objectives/{id}/tasks` | Get plan tasks |
| GET | `/composer/objectives/{id}/timeline` | Get event timeline |
| GET | `/composer/objectives/{id}/report` | Get HTML/JSON report |
| POST | `/composer/objectives/{id}/start` | Start pipeline |
| POST | `/composer/objectives/{id}/pause` | Pause execution |
| POST | `/composer/objectives/{id}/resume` | Resume execution |
| POST | `/composer/objectives/{id}/cancel` | Cancel objective |
| POST | `/composer/objectives/{id}/reconcile` | Reconcile progress |
| POST | `/composer/objectives/{id}/steer` | Add steering guidance |

All routes except `/health` and `/version` use Conductor authentication.

## MCP tools

12 MCP tools exposed through Conductor's MCP surface:

- `composer_submit_spec`
- `composer_list_objectives`
- `composer_get_objective`
- `composer_get_plan`
- `composer_get_status`
- `composer_get_timeline`
- `composer_get_report`
- `composer_pause`
- `composer_resume`
- `composer_cancel`
- `composer_reconcile`
- `composer_steer`

No tmux, worktree, or harness-level commands are exposed through MCP. Those
remain internal through Agents Gateway.

## Events

```
composer.objective_received
composer.spec_normalizing
composer.spec_normalized
composer.context_built
composer.plan_generated
composer.plan_validation_failed
composer.plan_repaired
composer.plan_validated
composer.plan_activated
composer.task_ready
composer.task_dispatching
composer.task_dispatched
composer.task_running
composer.task_waiting_for_reply
composer.interaction_received
composer.interaction_answered
composer.task_restarted
composer.task_verifying
composer.task_completed
composer.task_blocked_external
composer.integration_ready
composer.integration_dispatched
composer.integration_completed
composer.final_verification_started
composer.final_verification_passed
composer.report_generated
composer.objective_completed
composer.objective_blocked_external
composer.objective_failed
```

## Configuration

```bash
CONDUCTOR_COMPOSER_ENABLED=true
CONDUCTOR_COMPOSER_LLM_BASE_URL=https://openrouter.ai/api/v1
CONDUCTOR_COMPOSER_LLM_API_KEY=sk-or-v1-...
CONDUCTOR_COMPOSER_LLM_MODEL=deepseek/deepseek-chat-v3-2
CONDUCTOR_COMPOSER_LLM_TIMEOUT_SECONDS=180
CONDUCTOR_COMPOSER_MAX_PARALLEL_TASKS=3
CONDUCTOR_COMPOSER_POLL_INTERVAL_SECONDS=10
CONDUCTOR_COMPOSER_DEFAULT_HARNESS_PROFILE=opencode-deepseek
CONDUCTOR_COMPOSER_INTEGRATION_HARNESS_PROFILE=opencode-deepseek
CONDUCTOR_COMPOSER_AUTO_START=true
CONDUCTOR_COMPOSER_AUTO_COMMIT=true
CONDUCTOR_COMPOSER_AUTO_PUSH=false
CONDUCTOR_COMPOSER_AUTO_PR=false
CONDUCTOR_COMPOSER_REPORT_DIR=/var/lib/conductor/composer-reports
```

## Reports

Composer generates two reports per completed objective:

- `composer-reports/<objective_id>/review-report.html` — human morning review
- `composer-reports/<objective_id>/result.json` — machine-readable state

Reports include: task graph, dependency visualization, harness profiles, skills,
branches/commits, interaction decisions, verification matrix, test results,
integration branch, and morning summary.

**Secrets are never exposed** in reports. Credential-shaped substrings are
automatically redacted.

## External blockers

Valid reasons Composer will mark an objective `blocked_external`:

- Missing credentials (API keys, tokens)
- Required binary unavailable (harness profile not installed)
- Required external service unavailable (gateway down)
- Repository inaccessible
- Environment incapable of running required live verification

Failed tests are **not** an external blocker. Composer and Agents Gateway
continue working toward passing results.

## Verification contract

Completion requires:
- Every required plan node completed
- Integration task completed
- Full test suite passed
- Final report generated

## Known limitations (v1)

- No general-purpose code-review agent
- No web dashboard
- No containerized harness sessions
- No production auto-deployment
- No cost/token/wall-clock budgets
- Claude Code and Codex live proof not yet available