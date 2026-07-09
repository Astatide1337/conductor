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

echo "=== Conductor Local E2E Smoke ==="
echo ""

# 1. Health
echo "--- Health ---"
R=$(curl -sf "${BASE}/health")
check "health ok" "ok" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"

# 2. Version
echo "--- Version ---"
R=$(curl -sf "${BASE}/version")
check "version service" "astatide-conductor" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['service'])")"

# 3. Ready
echo "--- Ready ---"
R=$(curl -sf "${BASE}/ready")
check "ready" "True" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['ready'])")"

# 4. Create objective
echo "--- Create Objective ---"
R=$(curl -sf -X POST "${BASE}/objectives" -H "Content-Type: application/json" -d '{"title":"E2E Smoke Test","description":"Verify end-to-end flow"}')
OBJ_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective_id'])")
RUN_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['run_id'])")
check "objective created" "created" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"
echo "  objective_id=${OBJ_ID}"
echo "  run_id=${RUN_ID}"

# 5. Get objective
echo "--- Get Objective ---"
R=$(curl -sf "${BASE}/objectives/${OBJ_ID}")
check "get objective status" "created" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective']['status'])")"

# 6. Resume (activate) objective
echo "--- Resume Objective ---"
R=$(curl -sf -X POST "${BASE}/objectives/${OBJ_ID}/resume")
check "resume to active" "active" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective']['status'])")"

# 7. Create task
echo "--- Create Task ---"
R=$(curl -sf -X POST "${BASE}/objectives/${OBJ_ID}/tasks" -H "Content-Type: application/json" -d '{"title":"E2E Verify Task","task_type":"verify","required_skills":[]}')
TASK_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
check "task created" "created" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"
echo "  task_id=${TASK_ID}"

# 8. Get task
echo "--- Get Task ---"
R=$(curl -sf "${BASE}/tasks/${TASK_ID}")
check "get task title" "E2E Verify Task" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['task']['title'])")"

# 9. Dry run
echo "--- Dry Run ---"
R=$(curl -sf -X POST "${BASE}/dry-run")
DR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('would_dispatch', False))")
check "dry run returns" "False" "$DR"

# 10. Dispatch task
echo "--- Dispatch Task ---"
R=$(curl -sf -X POST "${BASE}/tasks/${TASK_ID}/dispatch")
AGENT_STATUS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
check "dispatch creates agent_run" "running" "$AGENT_STATUS"
echo "  agent_run_status=${AGENT_STATUS}"

# 11. Check task status after dispatch
echo "--- Task After Dispatch ---"
R=$(curl -sf "${BASE}/tasks/${TASK_ID}")
TASK_STATUS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['task']['status'])")
check "task running after dispatch" "running" "$TASK_STATUS"

# 12. List events
echo "--- Events ---"
R=$(curl -sf "${BASE}/events?objective_id=${OBJ_ID}")
EVENT_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])")
check "events exist" "True" "$(python3 -c "print(True if int('${EVENT_COUNT}') > 0 else False)")"
echo "  event_count=${EVENT_COUNT}"

# 13. List approvals (should be empty)
echo "--- Approvals ---"
R=$(curl -sf "${BASE}/approvals")
check "approvals empty" "0" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])")"

# 14. Pause objective
echo "--- Pause Objective ---"
R=$(curl -sf -X POST "${BASE}/objectives/${OBJ_ID}/pause")
check "paused" "paused" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective']['status'])")"

# 15. Resume objective
echo "--- Resume Objective ---"
R=$(curl -sf -X POST "${BASE}/objectives/${OBJ_ID}/resume")
check "resumed" "active" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective']['status'])")"

# 16. Complete objective
echo "--- Complete Objective ---"
R=$(curl -sf -X POST "${BASE}/objectives/${OBJ_ID}/resume")  # ensure active
R=$(curl -sf "${BASE}/tasks/${TASK_ID}")   # check task status

echo ""
echo "=== Results ==="
echo "Passed: ${PASS}"
echo "Failed: ${FAIL}"
echo ""

if [[ $FAIL -gt 0 ]]; then
    echo "❌ E2E smoke failed"
    exit 1
else
    echo "✅ E2E smoke passed"
    exit 0
fi