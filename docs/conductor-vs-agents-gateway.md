# Conductor vs Agents Gateway

Both services live in the Astatide platform. They have separate, non-overlapping
responsibilities. Together they form the orchestration → execution pipeline.

## Conductor

The Conductor is the **orchestration layer**.

| Responsibility | Examples |
|---|---|
| Objective lifecycle | Create, pause, resume, cancel objectives |
| Task graph | Define tasks with dependencies and required skills |
| Planner decisions | Decide what to do next (manual, deterministic, or LLM) |
| Approval queue | Gate irreversible actions behind human approval |
| Policy enforcement | Classify actions as autonomous, approval-needed, or denied |
| Circuit breakers | Halt unsafe loops (cost, iteration, concurrency limits) |
| Dispatch coordination | Send tasks to Agents Gateway with idempotency keys |
| Status aggregation | Collect status from all agent runs |
| Event ingestion | Append-only audit trail |
| Reconciliation | Repair state after restart |
| MCP cockpit surface | 15 high-level MCP tools for any cockpit |

**Conductor does NOT:**

- Run shell commands
- Create tmux sessions
- Execute agent tasks directly
- Write code
- Manage runtime containers

## Agents Gateway

Agents Gateway is the **execution layer**.

| Responsibility | Examples |
|---|---|
| Agent catalog | Discover and validate agent manifests |
| Task lifecycle | Create, queue, run, complete, fail tasks |
| Background worker | Poll for queued tasks and execute them |
| Runtime adapters | Stub, Process, Docker adapters |
| Task events | Per-task event log (started, progress, completed) |
| Task artifacts | Store and retrieve task output files |
| Runtime sandboxing | Docker limits (memory, CPU, PIDs, tmpfs) |

**Agents Gateway does NOT:**

- Plan what work to do
- Decide task priority or order
- Enforce approval policies
- Track objective-level progress
- Manage circuit breakers

## How they interact

```
User/Cockpit → Conductor
                  ↓ (dispatch with idempotency key)
              Agents Gateway
                  ↓ (background worker)
              Runtime adapter
                  ↓ (process/docker)
              Agent output → Agents Gateway (events, artifacts)
                  ↓ (reconciliation poll)
              Conductor (update status, collect results)
```

Conductor and Agents Gateway **must not absorb each other's functions**.

Conductor calls Agents Gateway via HTTP REST. Agents Gateway never calls
back to Conductor — reconciliation is pull-based (Conductor polls Agents
Gateway for status updates).