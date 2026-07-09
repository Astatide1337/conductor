# Planner — Design and modes

The Conductor supports three planner modes. Only the first two are implemented
today. The LLM planner is a future milestone and must operate under strict
policy and circuit breaker controls.

## Manual mode

The cockpit/user submits structured decisions. No automation.

This mode is used for:
- Initial testing and validation
- Capturing real decision traces before automating
- Manual orchestration of high-risk workflows

Decisions are validated against the PlannerDecision schema and must pass
policy checks before execution.

## Deterministic mode (implemented)

Rule-based automation without any LLM. Simple, predictable, safe.

### Rules

| Condition | Decision |
|---|---|
| Run has no tasks | `create_tasks` — block until tasks exist |
| Ready tasks, concurrency available | `dispatch_task` — dispatch next ready task |
| All tasks completed, no failures | `mark_objective_complete` — complete the run |
| All tasks completed, some failed | `mark_objective_blocked` — block with failure |
| Tasks in `created` or `blocked` state | Move to `ready` for next dispatch |
| Circuit breaker tripped | `request_approval` — escalate to human |
| Objective paused/blocked/terminal | `do_nothing` |
| At max concurrency | `do_nothing` — wait |

### Decision lifecycle

```
1. Load objective run status
2. Check circuit breakers first
3. Evaluate tasks:
   a. Ready tasks with available concurrency → dispatch
   b. Failed tasks within retry limit → retry
   c. All complete → complete run
   d. Stuck tasks → move to ready
4. Return PlannerDecision
5. Decision passes through policy check
6. Execute decision (dispatch / status update / approval request)
```

## LLM mode (future milestone)

The LLM planner calls an external API to propose structured decisions.
Every decision must pass through the same policy and circuit breaker
checks as deterministic decisions.

### Provider abstraction

```python
class BasePlannerProvider:
    def propose(self, context: dict) -> PlannerDecision:
        ...
```

### Safety requirements

1. LLM output must be valid JSON matching the PlannerDecision schema
2. Invalid output → retry once, then escalate
3. Every decision goes through `check_decision()` policy
4. Every decision goes through circuit breaker evaluation
5. All decisions recorded in `planner_turns` table
6. Never allow raw LLM text to directly mutate state
7. LLM is advisory only — policy gates execute the decision

### Decision schema

```json
{
  "decision_type": "dispatch_task",
  "reason": "The auth task is ready and has no dependencies.",
  "task_id": "optional-uuid",
  "new_tasks": [],
  "approval_request": null,
  "guidance": null,
  "confidence": 0.82
}
```

Allowed `decision_type` values:
- `create_tasks`
- `dispatch_task`
- `retry_task`
- `request_approval`
- `mark_task_blocked`
- `mark_objective_blocked`
- `mark_objective_complete`
- `pause_objective`
- `do_nothing`

Invalid or unexpected decision types → `requires_approval` policy verdict.

## Policy integration

Planner decisions are NOT executed directly. The flow is:

```
Planner produces PlannerDecision
    ↓
check_decision(decision_type) → PolicyResult
    ↓
If allowed_autonomous → execute
If requires_approval → create approval, emit event
If denied → emit event, do nothing
```

Every decision is:
- Logged to events
- Recorded in planner_turns
- Validated against circuit breakers

Planner output is advisory, not authoritative. Policy has final say.