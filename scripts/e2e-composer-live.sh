#!/usr/bin/env bash
# Live Composer E2E — spec-to-verified-execution with real Composer LLM,
# real Agents Gateway, real opencode-deepseek harness.
#
# Requires:
#   - Conductor running with Composer enabled and an LLM API key configured
#   - Agents Gateway running with the opencode-deepseek harness profile
#
# The live script submits a finalized calculator spec, waits for Composer to
# normalize, plan, dispatch, integrate, verify, and produce a final report.
#
# See docs/composer-e2e.md for usage, expected output, and known blockers.
set -euo pipefail

# ── Required environment ───────────────────────────────────────────────────
REQUIRED_ENV=(
    CONDUCTOR_BASE_URL
    CONDUCTOR_AUTH_MODE
    CONDUCTOR_INTERNAL_TOKEN
)
REQUIRED_COMPOSER_ENV=(
    CONDUCTOR_COMPOSER_LLM_BASE_URL
    CONDUCTOR_COMPOSER_LLM_API_KEY
    CONDUCTOR_COMPOSER_LLM_MODEL
)
REQUIRED_AGENTS_ENV=(
    CONDUCTOR_AGENTS_GATEWAY_URL
    CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE
    CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN
)
REQUIRED_SKILLS_ENV=(
    CONDUCTOR_SKILLS_GATEWAY_URL
    CONDUCTOR_SKILLS_GATEWAY_AUTH_MODE
    CONDUCTOR_SKILLS_GATEWAY_INTERNAL_TOKEN
)
OPTIONAL_MCP_ENV=(
    CONDUCTOR_MCP_GATEWAY_URL
    CONDUCTOR_MCP_GATEWAY_AUTH_MODE
    CONDUCTOR_MCP_GATEWAY_INTERNAL_TOKEN
)

MISSING=()
for v in "${REQUIRED_ENV[@]}" "${REQUIRED_COMPOSER_ENV[@]}" "${REQUIRED_AGENTS_ENV[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        MISSING+=("$v")
    fi
done

SKILLS_MISSING=()
for v in "${REQUIRED_SKILLS_ENV[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        SKILLS_MISSING+=("$v")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "COMPOSER LIVE E2E BLOCKED: missing ${MISSING[*]}"
    echo ""
    echo "Set these variables and re-run:"
    for v in "${MISSING[@]}"; do
        echo "  export $v=..."
    done
    echo ""
    if [[ ${#SKILLS_MISSING[@]} -gt 0 ]]; then
        echo "Skills Gateway (required for skill validation):"
        for v in "${SKILLS_MISSING[@]}"; do
            echo "  export $v=..."
        done
    fi
    echo ""
    echo "Optional (MCP Gateway):"
    for v in "${OPTIONAL_MCP_ENV[@]}"; do
        echo "  export $v=..."
    done
    exit 0
fi

BASE="${CONDUCTOR_BASE_URL}"
AUTH_HEADER="X-Auth-Internal-Token: ${CONDUCTOR_INTERNAL_TOKEN}"

TIMEOUT_SEC="${COMPOSER_LIVE_TIMEOUT_SEC:-600}"
PASS=0
FAIL=0
STAGE=""

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

fail_stage() {
    local msg="$1"
    echo ""
    echo "COMPOSER LIVE E2E FAILED: ${STAGE} — ${msg}"
    exit 1
}

poll_until() {
    local desc="$1" max_sec="$2" url="$3" grep_pattern="$4" status_field="$5" expected="$6"
    STAGE="${desc}"
    local waited=0
    while [[ $waited -lt $max_sec ]]; do
        local R
        R=$(curl -sf -H "${AUTH_HEADER}" "${url}" 2>/dev/null || echo '{"error":"curl failed"}')
        local val
        val=$(echo "$R" | python3 -c "
import sys,json
d = json.load(sys.stdin)
if '${grep_pattern}' == 'CONTAINS':
    text = json.dumps(d)
    print('contains' if '${expected}' in text else '')
else:
    print(d.get('${status_field}',''))
" 2>/dev/null || echo '')
        if [[ "$val" == "$expected" || "$val" == "contains" ]]; then
            echo "  OK: ${desc} (after ${waited}s)"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    echo "  TIMEOUT: ${desc} after ${waited}s"
    echo "  Last response: ${R}"
    echo ""
    echo "COMPOSER LIVE E2E TIMED OUT: ${STAGE}"
    exit 1
}

echo "=== Composer Live E2E ==="
echo ""
echo "Conductor: ${BASE}"
echo "Composer LLM: ${CONDUCTOR_COMPOSER_LLM_MODEL}"
echo "Agents Gateway: ${CONDUCTOR_AGENTS_GATEWAY_URL}"
echo "Timeout: ${TIMEOUT_SEC}s"
echo ""

# ── 1. Health ─────────────────────────────────────────────────────────────
STAGE="health"
echo "--- Health ---"
R=$(curl -sf "${BASE}/health")
check "health ok" "ok" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"

# ── 2. Submit finalized calculator spec ───────────────────────────────────
STAGE="submit spec"
echo "--- Submit Spec ---"
SPEC_TEXT='Extend the calculator package.

Requirements:
1. Add multiply(a, b).
2. Add divide(a, b).
3. divide must raise ValueError when b is zero.
4. Add complete pytest coverage.
5. Preserve add(a, b).
6. Update README with usage examples.
7. Run the full pytest suite.
8. Produce a final integrated branch containing all changes.
9. Produce an HTML review report with verification evidence.'

R=$(curl -sf -X POST "${BASE}/composer/objectives" \
    -H "Content-Type: application/json" \
    -H "${AUTH_HEADER}" \
    -d "$(python3 -c "
import json
print(json.dumps({
    'title': 'Live E2E Calculator Extension',
    'spec': '''${SPEC_TEXT}''',
    'auto_start': True,
}))
")")
OBJ_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective_id'])")
SPEC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['composer_spec_id'])")
check "spec submitted" "received" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"
echo "  objective_id=${OBJ_ID}"
echo "  composer_spec_id=${SPEC_ID}"

# ── 3. Wait for spec normalization ────────────────────────────────────────
poll_until "spec normalized" 60 "${BASE}/composer/objectives/${OBJ_ID}/spec" \
    "normalized\|planned\|planning\|executing" "status" "normalized"

# ── 4. Wait for plan generation ───────────────────────────────────────────
poll_until "plan generated" 120 "${BASE}/composer/objectives/${OBJ_ID}/plan" \
    "CONTAINS" "plan" "active"

# ── 5. Verify plan has at least 2 implementation tasks ────────────────────
STAGE="verify plan"
echo "--- Verify Plan ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/plan")
TASK_COUNT=$(echo "$R" | python3 -c "
import sys,json
plan = json.load(sys.stdin)
tasks = plan.get('plan_tasks', [])
impl = [t for t in tasks if t.get('node_key') != 'integration']
print(len(impl))
")
check_contains "at least 2 impl tasks" "2\|3\|4\|5" "${TASK_COUNT}"

# ── 6. Wait for tasks to dispatch ─────────────────────────────────────────
poll_until "tasks dispatched" 120 "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
    "CONTAINS" "dispatching\|running\|completed" "contains"

# ── 7. Wait for at least 2 tasks to complete ──────────────────────────────
poll_until "tasks completing" $((TIMEOUT_SEC - 200)) "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
    "CONTAINS" "completed" "contains"

# ── 8. Wait for integration task to be created and dispatched ─────────────
poll_until "integration started" 120 "${BASE}/composer/objectives/${OBJ_ID}/plan" \
    "CONTAINS" "integration" "contains"

# ── 9. Wait for integration to complete ───────────────────────────────────
poll_until "integration completed" $((TIMEOUT_SEC - 100)) "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
    "completed" "status" "completed"

# ── 10. Final report generated ────────────────────────────────────────────
STAGE="check report"
echo "--- Check Report ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/report")
REPORT_STATUS=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))")
check "report generated" "completed" "${REPORT_STATUS}"

# ── 11. Objective completed ───────────────────────────────────────────────
STAGE="check completion"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}")
OBJ_STATUS=$(echo "$R" | python3 -c "
import sys,json
d = json.load(sys.stdin)
spec = d.get('composer_spec', {})
print(spec.get('status',''))
")
check "objective completed" "completed" "${OBJ_STATUS}"

echo ""
echo "=== Composer Live E2E Complete ==="
echo ""
echo "Passed: ${PASS}"
echo "Failed: ${FAIL}"

if [ "$FAIL" -eq 0 ]; then
    echo "COMPOSER LIVE E2E PASSED"
    exit 0
else
    echo "COMPOSER LIVE E2E FAILED: ${STAGE}"
    exit 1
fi