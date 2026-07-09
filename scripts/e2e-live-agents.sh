#!/usr/bin/env bash
# Live E2E against real gateway infrastructure.
#
# Exercises the full Conductor -> Agents Gateway -> Skills Gateway path. Does
# NOT fake HTTP. Requires credentials; exits with a clear message listing the
# missing variables if any are unset. Use scripts/e2e-local.sh for the
# offline (mock) smoke.
#
# See docs/live-e2e.md for usage, expected output, and known blockers.
set -euo pipefail

# ── Required environment ───────────────────────────────────────────────────
# Required to talk to Conductor itself.
REQUIRED_ENV=(
    CONDUCTOR_BASE_URL
    CONDUCTOR_AUTH_MODE
    CONDUCTOR_INTERNAL_TOKEN
)
# Required for Conductor to talk to the Agents Gateway.
REQUIRED_AGENTS_ENV=(
    CONDUCTOR_AGENTS_GATEWAY_URL
    CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE
    CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN
)
# Optional — Skills Gateway may be skipped if validation is no-op.
OPTIONAL_SKILLS_ENV=(
    CONDUCTOR_SKILLS_GATEWAY_URL
    CONDUCTOR_SKILLS_GATEWAY_AUTH_MODE
    CONDUCTOR_SKILLS_GATEWAY_INTERNAL_TOKEN
)

MISSING=()
for v in "${REQUIRED_ENV[@]}" "${REQUIRED_AGENTS_ENV[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        MISSING+=("$v")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "LIVE E2E BLOCKED: missing ${MISSING[*]}"
    echo ""
    echo "Set these variables and re-run:"
    for v in "${MISSING[@]}"; do
        echo "  export $v=..."
    done
    echo ""
    echo "Optional (Skills Gateway validation):"
    for v in "${OPTIONAL_SKILLS_ENV[@]}"; do
        echo "  export $v=..."
    done
    exit 2
fi

# ── Derived state ───────────────────────────────────────────────────────────
BASE="${CONDUCTOR_BASE_URL%/}"  # strip trailing slash if present
AUTH_HEADERS=()

case "${CONDUCTOR_AUTH_MODE}" in
    internal-only|cloudflare-access)
        AUTH_HEADERS+=("-H" "X-Auth-Internal-Token: ${CONDUCTOR_INTERNAL_TOKEN}")
        ;;
    dev-none)
        : # no header
        ;;
    *)
        echo "ERROR: unknown CONDUCTOR_AUTH_MODE='${CONDUCTOR_AUTH_MODE}'"
        exit 2
        ;;
esac

if [[ "${CONDUCTOR_AUTH_MODE}" == "cloudflare-access" && -n "${CONDUCTOR_CF_ACCESS_JWT:-}" ]]; then
    AUTH_HEADERS+=("-H" "Cf-Access-Jwt-Assertion: ${CONDUCTOR_CF_ACCESS_JWT}")
fi

PASS=0
FAIL=0
TRANSSCRIPT=()

check() {
    local label="$1" expected="$2" got="$3"
    if [[ "$got" == "$expected" ]]; then
        echo "  PASS: ${label}"
        PASS=$((PASS + 1))
        TRANSSCRIPT+=("PASS: ${label}")
    else
        echo "  FAIL: ${label} (expected '${expected}', got '${got}')"
        FAIL=$((FAIL + 1))
        TRANSSCRIPT+=("FAIL: ${label} (expected '${expected}', got '${got}')")
    fi
}

echo "=== Conductor Live E2E (Agents Gateway + Skills Gateway) ==="
echo ""
echo "Conductor: ${BASE} (auth=${CONDUCTOR_AUTH_MODE})"
echo "Agents Gateway: ${CONDUCTOR_AGENTS_GATEWAY_URL} (auth=${CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE})"
echo "Skills Gateway: ${CONDUCTOR_SKILLS_GATEWAY_URL:-<not configured>}"
echo ""

# Helper: fetch a URL into a file, print the HTTP status code.
# fetch_to <out_file> <method> <url> [payload]
fetch_to() {
    local out="$1"; shift
    local method="$1"; shift
    local url="$1"; shift
    local status
    if [[ "$method" == "GET" ]]; then
        status=$(curl -s -o "$out" -w "%{http_code}" "${AUTH_HEADERS[@]}" "$url" 2>/dev/null || echo "000")
    else
        local payload="${1:-}"
        status=$(curl -s -o "$out" -w "%{http_code}" "${AUTH_HEADERS[@]}" -X "$method" "$url" \
            -H "Content-Type: application/json" -d "$payload" 2>/dev/null || echo "000")
    fi
    echo "$status"
}

# ── Step 1: Health ─────────────────────────────────────────────────────────
echo "--- Health ---"
RESP=$(fetch_to /tmp/conductor_e2e_health.json GET "${BASE}/health")
check "health endpoint reachable" "200" "$RESP"
if [[ "$RESP" != "200" ]]; then
    echo "FATAL: Conductor unreachable at ${BASE}/health"
    exit 1
fi
STATUS=$(python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_health.json'))['status'])")
check "health status ok" "ok" "$STATUS"

# ── Step 2: Version ────────────────────────────────────────────────────────
echo "--- Version ---"
RESP=$(fetch_to /tmp/conductor_e2e_version.json GET "${BASE}/version")
check "version endpoint reachable" "200" "$RESP"

# ── Step 3: Create an objective ────────────────────────────────────────────
echo "--- Create Objective ---"
OBJ_PAYLOAD='{"title":"live-e2e","description":"Live Agents Gateway smoke","priority":"normal"}'
RESP=$(fetch_to /tmp/conductor_e2e_obj.json POST "${BASE}/objectives" "$OBJ_PAYLOAD")
check "create objective returns 201" "201" "$RESP"
OBJ_ID=$(python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_obj.json'))['objective_id'])")
RUN_ID=$(python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_obj.json'))['run_id'])")
echo "  objective_id=${OBJ_ID} run_id=${RUN_ID}"

# ── Step 4: Create a task with NO required skills (deferred skills validation) ──
echo "--- Create Task (no required_skills) ---"
TASK_PAYLOAD='{"title":"live-e2e task","brief":"Probe Agents Gateway end-to-end"}'
RESP=$(fetch_to /tmp/conductor_e2e_task.json POST "${BASE}/objectives/${OBJ_ID}/tasks" "$TASK_PAYLOAD")
check "create task returns 201" "201" "$RESP"
TASK_ID=$(python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_task.json'))['id'])" 2>/dev/null \
    || python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_task.json'))['task_id'])")
echo "  task_id=${TASK_ID}"

# ── Step 5: Dispatch the task ──────────────────────────────────────────────
echo "--- Dispatch Task ---"
RESP=$(fetch_to /tmp/conductor_e2e_dispatch.json POST "${BASE}/tasks/${TASK_ID}/dispatch" "{}")
if [[ "$RESP" == "200" ]]; then
    check "dispatch returns 200" "200" "$RESP"
    AR_STATUS=$(python3 -c "import json; d=json.load(open('/tmp/conductor_e2e_dispatch.json')); print(d.get('status','-'))" 2>/dev/null || echo "?")
    AR_ID=$(python3 -c "import json; d=json.load(open('/tmp/conductor_e2e_dispatch.json')); ar=d.get('agent_run') or d; print((ar or {}).get('id','-'))" 2>/dev/null || echo "-")
    echo "  agent_run_id=${AR_ID} status=${AR_STATUS}"
elif [[ "$RESP" == "000" ]]; then
    echo "  FAIL: dispatch request failed (transport error)"
    FAIL=$((FAIL + 1))
    AR_ID=""
else
    ERR=$(python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_dispatch.json')).get('error','-'))" 2>/dev/null || echo "?")
    echo "  WARN: dispatch returned status=$RESP (error=${ERR})"
    check "dispatch returned 2xx" "200" "$RESP"
    AR_ID=""
fi

# ── Step 6: Poll Conductor status ──────────────────────────────────────────
echo "--- Poll Task Status ---"
RESP=$(fetch_to /tmp/conductor_e2e_status.json GET "${BASE}/tasks?objective_id=${OBJ_ID}")
if [[ "$RESP" == "200" ]]; then
    COUNT=$(python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_status.json'))['count'])" 2>/dev/null || echo "?")
    echo "  task list count=${COUNT}"
    check "task list reachable" "200" "$RESP"
else
    echo "  WARN: task list returned ${RESP}"
fi

# ── Step 7: Reconcile ──────────────────────────────────────────────────────
echo "--- Reconcile ---"
RESP=$(fetch_to /tmp/conductor_e2e_reconcile.json POST "${BASE}/reconcile")
check "reconcile returns 200" "200" "$RESP"
if [[ "$RESP" == "200" ]]; then
    RECONCILED=$(python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_reconcile.json'))['reconciled'])" 2>/dev/null || echo "?")
    TRANSITIONS=$(python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_reconcile.json'))['transitions'])" 2>/dev/null || echo "?")
    ERRORS=$(python3 -c "import json; print(json.load(open('/tmp/conductor_e2e_reconcile.json'))['errors'])" 2>/dev/null || echo "?")
    echo "  reconciled=${RECONCILED} transitions=${TRANSITIONS} errors=${ERRORS}"
fi

# ── Step 8: Fetch events ────────────────────────────────────────────────────
echo "--- Events ---"
RESP=$(fetch_to /tmp/conductor_e2e_events.json GET "${BASE}/events?objective_id=${OBJ_ID}&limit=50")
if [[ "$RESP" == "200" ]]; then
    EVENTS=$(python3 -c "import json; d=json.load(open('/tmp/conductor_e2e_events.json')); print(len(d.get('events', d if isinstance(d, list) else [])))" 2>/dev/null || echo "0")
    echo "  event_count=${EVENTS}"
else
    echo "  WARN: events endpoint returned ${RESP}"
    EVENTS="0"
fi
if [[ "${EVENTS:-0}" -ge 1 ]]; then
    PASS=$((PASS + 1))
    echo "  PASS: events exist for new objective"
    TRANSSCRIPT+=("PASS: events exist for new objective")
else
    FAIL=$((FAIL + 1))
    echo "  FAIL: no events emitted for objective lifecycle"
    TRANSSCRIPT+=("FAIL: no events emitted for objective lifecycle")
fi

# ── Step 9: Fetch agent_run status ──────────────────────────────────────────
echo "--- Agent Run Status ---"
if [[ -n "${AR_ID:-}" && "${AR_ID}" != "-" && "${AR_ID}" != "" ]]; then
    python3 -c "
import json, sys
data = json.load(open('/tmp/conductor_e2e_dispatch.json'))
ar = data.get('agent_run') or data
status = ar.get('status', '-'); gw_id = ar.get('agents_gateway_task_id', '-')
print(f'  agent_run status={status} agents_gateway_task_id={gw_id}')"
else
    echo "  (no agent_run to inspect — dispatch never produced one)"
fi

# ── Step 10: Confirm artifact ingestion if artifacts exist ─────────────────
echo "--- Artifacts ---"
if [[ "${EVENTS:-0}" -gt 0 ]]; then
    python3 -c "
import json
data = json.load(open('/tmp/conductor_e2e_events.json'))
evts = data if isinstance(data, list) else data.get('events', [])
art_evts = [e for e in evts if 'artifact' in (e.get('event_type','') if isinstance(e, dict) else '')]
print(f'  artifact events observed: {len(art_evts)}')
for e in art_evts[:5]:
    print(f'    {e.get(\"event_type\")}: {e.get(\"message\",\"\")[:80]}')
"
else
    echo "  (no events to inspect for artifact ingestion)"
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "=== Results ==="
echo "Passed: ${PASS}"
echo "Failed: ${FAIL}"

if [[ ${FAIL} -eq 0 ]]; then
    echo "✅ Live E2E passed"
    exit 0
else
    echo "❌ Live E2E had failures"
    echo ""
    echo "Transcript:"
    for line in "${TRANSSCRIPT[@]}"; do
        echo "  ${line}"
    done
    exit 1
fi
