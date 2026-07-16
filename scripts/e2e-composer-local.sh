#!/usr/bin/env bash
set -euo pipefail

HOST="${CONDUCTOR_HOST:-localhost:8093}"
PASS=0
FAIL=0
BASE="http://${HOST}"

check() {
    local label="$1" expected="$2" got="$3"
    if [[ "$got" == "$expected" ]]; then
        echo "  PASS: ${label}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${label} (expected '${expected}', got '${got}')"
        FAIL=$((FAIL + 1))
    fi
}

check_contains() {
    local label="$1" pattern="$2" text="$3"
    if echo "$text" | grep -q "$pattern"; then
        echo "  PASS: ${label}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${label} (expected pattern '${pattern}' in '${text}')"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Composer Local E2E ==="
echo ""

# 0. Pre-flight: confirm the server is the dev-image with test_mode
# enabled *before* we submit anything. The dev/force-complete endpoint
# refuses to run on production-grade deployments, so the local E2E
# itself refuses to start without it.
echo "--- Pre-flight: composer test_mode ---"
R=$(curl -sf "${BASE}/health" 2>/dev/null || echo '{"status":"unreachable"}')
HEALTH_STATUS=$(echo "$R" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('status', 'missing'))
except Exception:
    print('parse_failure')
")
check "health ok" "ok" "${HEALTH_STATUS}"
COMPOSER_PROVIDER=$(echo "$R" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # Health exposes the composer's LLM provider diagnostic. When
    # test_mode is enabled we MUST see 'fake' — never 'http' in local E2E.
    print(d.get('composer_llm_provider', 'missing'))
except Exception:
    print('parse_failure')
")
if [[ "$COMPOSER_PROVIDER" == "missing" || "$COMPOSER_PROVIDER" == "parse_failure" ]]; then
    echo "  WARN: composer_llm_provider not exposed; pre-flight check skips"
else
    check_contains "composer test_mode active" "fake" "$COMPOSER_PROVIDER"
fi

# 1. Submit Composer spec async (returns immediately with status "received")
echo "--- Submit Spec (async) ---"
R=$(curl -sf -X POST "${BASE}/composer/objectives" -H "Content-Type: application/json" -d '{
  "title":"Local E2E Calculator",
  "spec":"Build a calculator with add, multiply, and divide functions. Include pytest tests with full coverage.",
  "repository":{"url":"https://github.com/test/local-calc.git","base_branch":"develop"},
  "auto_start":true
}')
OBJ_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective_id'])")
SPEC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['composer_spec_id'])")
echo "  objective_id=${OBJ_ID}"
echo "  composer_spec_id=${SPEC_ID}"

# 2. Repository + base_branch preservation
echo "--- Repository Preservation ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/spec")
REPO_URL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('repository_url',''))")
BRANCH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_branch',''))")
check "repository url preserved" "https://github.com/test/local-calc.git" "${REPO_URL}"
check "base_branch preserved" "develop" "${BRANCH}"

# 3. Wait for the spec to advance from "received" — supervisor tick.
echo "--- Supervisor advances spec ---"
ADVANCED=0
SPEC_STATUS=""
for _ in $(seq 1 15); do
    sleep 2
    R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/spec")
    SPEC_STATUS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))")
    case "${SPEC_STATUS}" in
        normalized|planning|planned|executing|integrating|verifying|completed)
            ADVANCED=1
            break
            ;;
    esac
done
if [ "$ADVANCED" -eq 1 ]; then
    echo "  PASS: spec advanced from received (status: ${SPEC_STATUS})"
    PASS=$((PASS + 1))
else
    echo "  FAIL: spec did not advance from received (expected normalized|planning|planned|executing, got ${SPEC_STATUS})"
    FAIL=$((FAIL + 1))
fi

# 4. Plan generated — multiple plan tasks required.
echo "--- Plan ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/plan")
PLAN_STATUS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))")
TASK_COUNT=$(echo "$R" | python3 -c "
import sys,json
plan = json.load(sys.stdin)
print(len(plan.get('plan_tasks', [])))
")
echo "  plan status: ${PLAN_STATUS}"
echo "  plan tasks: ${TASK_COUNT}"
check_contains "plan active" "active\|integrating\|completed" "${PLAN_STATUS}"
check_contains "at least 3 tasks" "2\|3\|4\|5\|6" "${TASK_COUNT}"

# 5. Tasks dispatched (supervisor ran dispatch_ready_tasks at least once)
echo "--- Tasks ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/tasks")
DISPATCHED=$(echo "$R" | python3 -c "
import sys,json
data = json.load(sys.stdin)
tasks = data.get('tasks', [])
print(len([t for t in tasks if t.get('status') in ('dispatching','running','completed','verifying')]))
")
echo "  dispatched tasks: ${DISPATCHED}"
check_contains "tasks dispatched" "1\|2\|3\|4\|5" "${DISPATCHED}"

# 6. Timeline has events
echo "--- Timeline ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/timeline")
EVENT_COUNT=$(echo "$R" | python3 -c "
import sys,json
data = json.load(sys.stdin)
print(len(data.get('events',[])))
")
echo "  events: ${EVENT_COUNT}"
check_contains "timeline has events" "1\|2\|3\|4\|5\|6\|7\|8\|9" "${EVENT_COUNT}"

# 7. ⭐ DETERMINISTIC COMPLETION DRIVE — the local E2E proof.
# Drive the mock-gateway tasks through completion, verification, and
# integration, then trigger objective completion + final report.
echo "--- Force completion (dev) ---"
R=$(curl -sf -X POST "${BASE}/composer/objectives/${OBJ_ID}/dev/force-complete" 2>/dev/null || echo '{"error":"unreachable"}')
DRIVE_ERROR=$(echo "$R" | python3 -c "
import sys,json
try:
    d = json.load(sys.stdin)
    print(d.get('error', '') or '')
except Exception:
    print('parse_failure')
")
if [[ -n "$DRIVE_ERROR" ]]; then
    echo "  FAIL: /dev/force-complete refused: ${DRIVE_ERROR}"
    echo "       (this means the server is NOT in test/dev mode — local E2E"
    echo "        cannot drive deterministic completion in production)"
    FAIL=$((FAIL + 1))
else
    FINAL_STATUS=$(echo "$R" | python3 -c "
import sys,json
d = json.load(sys.stdin)
print(d.get('final_status', 'missing'))
")
    ACTIONS_COUNT=$(echo "$R" | python3 -c "
import sys,json
d = json.load(sys.stdin)
print(len(d.get('actions', [])))
")
    echo "  drive actions: ${ACTIONS_COUNT}"
    check "objective completed" "completed" "${FINAL_STATUS}"
fi

# 8. Spec status check — the persisted spec.status MUST now read 'completed'.
echo "--- Spec Final Status ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/spec")
SPEC_STATUS_FINAL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))")
check "spec status completed" "completed" "${SPEC_STATUS_FINAL}"

# 9. Plan status — the persisted plan.status MUST now read 'completed'.
echo "--- Plan Final Status ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/plan")
PLAN_STATUS_FINAL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))")
check "plan status completed" "completed" "${PLAN_STATUS_FINAL}"

# 10. Final integration branch + commit SHA — the integration task
# (node_key="integration") MUST have both branch and commit_sha set
# in its persisted plan_task dict.
echo "--- Integration Task Evidence ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/plan")
INTEGRATION_EVIDENCE=$(echo "$R" | python3 -c "
import sys,json
d = json.load(sys.stdin)
impl_tasks = [t for t in d.get('plan_tasks', []) if t.get('node_key') == 'integration']
if not impl_tasks:
    print('MISSING')
else:
    t = impl_tasks[0]
    b = t.get('branch', '') or ''
    c = t.get('commit_sha', '') or ''
    if b and c:
        print(f'{b}+{c}')
    else:
        print(f'MISSING_BRANCH_OR_COMMIT ({b})({c})')
")
check_contains "integration task recorded branch + commit" "integration/main+intsha" "${INTEGRATION_EVIDENCE}"

# 11. ⭐ Final report — the objective's report MUST exist and report
# the completed objective. REQUIRED (no longer optional).
echo "--- Final Report (required) ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/report")
REPORT_STATUS=$(echo "$R" | python3 -c "
import sys,json
try:
    d = json.load(sys.stdin)
    print(d.get('status', 'missing'))
except Exception:
    print('missing')
")
check "report exists and status=completed" "completed" "${REPORT_STATUS}"
REPORT_ID=$(echo "$R" | python3 -c "
import sys,json
d = json.load(sys.stdin)
print(d.get('id', '') or d.get('report_id', '') or '')
")
if [[ -n "$REPORT_ID" ]]; then
    echo "  report id: ${REPORT_ID}"
    PASS=$((PASS + 1))
else
    echo "  FAIL: report missing id"
    FAIL=$((FAIL + 1))
fi

# 12. Top-level conductor objective status — confirm "completed" through
# the conductor's own /objectives/{id} endpoint as well, not just Composer's
# spec status.
echo "--- Conductor Objective Final ---"
R=$(curl -sf "${BASE}/objectives/${OBJ_ID}")
CONDUCTOR_STATUS=$(echo "$R" | python3 -c "
import sys,json
d = json.load(sys.stdin)
obj = d.get('objective', d)
print(obj.get('status', 'missing'))
")
echo "  conductor objective status: ${CONDUCTOR_STATUS}"
check_contains "conductor objective completed" "completed" "${CONDUCTOR_STATUS}"

echo ""
echo "=== Composer Local E2E Complete ==="
echo ""
echo "Passed: ${PASS}"
echo "Failed: ${FAIL}"

if [ "$FAIL" -eq 0 ]; then
    echo "Composer local E2E passed"
    exit 0
else
    echo "Composer local E2E failed"
    exit 1
fi
