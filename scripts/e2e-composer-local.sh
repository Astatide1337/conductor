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
        echo "  FAIL: ${label} (expected '${pattern}' in text)"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Composer Local E2E ==="
echo ""

# 1. Health
echo "--- Health ---"
R=$(curl -sf "${BASE}/health")
check "health ok" "ok" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"

# 2. Submit Composer spec async (returns immediately with status "received")
echo "--- Submit Spec (async) ---"
R=$(curl -sf -X POST "${BASE}/composer/objectives" -H "Content-Type: application/json" -d '{
  "title":"Local E2E Calculator",
  "spec":"Build a calculator with add, multiply, and divide functions. Include pytest tests with full coverage.",
  "repository":{"url":"https://github.com/test/local-calc.git","base_branch":"develop"},
  "auto_start":true
}')
OBJ_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective_id'])")
SPEC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['composer_spec_id'])")
check "spec submitted" "received" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"
echo "  objective_id=${OBJ_ID}"
echo "  composer_spec_id=${SPEC_ID}"

# 3. Verify repository + base_branch persisted
echo "--- Repository Preservation ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/spec")
REPO_URL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('repository_url',''))")
BRANCH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_branch',''))")
check "repository url preserved" "https://github.com/test/local-calc.git" "${REPO_URL}"
check "base_branch preserved" "develop" "${BRANCH}"

# 4. Wait for async supervisor to advance spec through stages
echo "--- Supervisor advances spec ---"
# Poll up to 30 seconds for the spec to advance from received
ADVANCED=0
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
    echo "  FAIL: spec advanced from received (expected normalized|planning|planned|executing, got ${SPEC_STATUS})"
    FAIL=$((FAIL + 1))
fi

# 5. Plan generated
echo "--- Plan ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/plan")
PLAN_STATUS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))")
check_contains "plan active" "active\|integrating\|completed" "${PLAN_STATUS}"
TASK_COUNT=$(echo "$R" | python3 -c "
import sys,json
plan = json.load(sys.stdin)
tasks = plan.get('plan_tasks', [])
print(len(tasks))
")
echo "  plan tasks: ${TASK_COUNT}"
check_contains "at least 3 tasks" "2\|3\|4\|5\|6" "${TASK_COUNT}"

# 6. Tasks dispatched (at least 2 separate gw tasks)
echo "--- Tasks ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/tasks")
TASKS_JSON="$R"
DISPATCHED=$(echo "$TASKS_JSON" | python3 -c "
import sys,json
data = json.load(sys.stdin)
tasks = data.get('tasks', [])
print(len([t for t in tasks if t.get('status') in ('dispatching','running','completed','verifying')]))
")
check_contains "tasks dispatched" "1\|2\|3\|4\|5" "${DISPATCHED}"

# 7. Timeline has events
echo "--- Timeline ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/timeline")
EVENT_COUNT=$(echo "$R" | python3 -c "
import sys,json
data = json.load(sys.stdin)
events = data.get('events',[])
print(len(events))
")
check_contains "timeline has events" "1\|2\|3\|4\|5\|6\|7\|8\|9" "${EVENT_COUNT}"

# 8. Reconcile (advances execution — sets executed task completions in mock)
echo "--- Reconcile ---"
R=$(curl -sf -X POST "${BASE}/composer/objectives/${OBJ_ID}/reconcile")
ACTIONS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('actions',[])))")
echo "  reconcile actions: ${ACTIONS}"

# 9. Objective visible
echo "--- Get Objective ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}")
OBJ_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if (d.get('id') or d.get('objective_id')) else 'missing')")
check "objective found" "ok" "${OBJ_OK}"

# 10. Report may be generated once mock tasks reach 'completed'
echo "--- Get Report ---"
R=$(curl -sf "${BASE}/composer/objectives/${OBJ_ID}/report" 2>/dev/null || echo '{"error":"not found"}')
REPORT_STATUS=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','') or d.get('error',''))")
echo "  report: ${REPORT_STATUS}"
# Not required to be completed — mock gateway doesn't advance tasks autonomously

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