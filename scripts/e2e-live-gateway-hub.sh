#!/usr/bin/env bash
# Live Gateway Hub E2E — exercises the full Conductor downstream path against
# real gateway infrastructure (Agents Gateway, Skills Gateway, MCP Gateway,
# optional wiki-mcp).
#
# Refuses to fake live E2E. If any required env var is missing, exits with code
# 2 and prints the exact variable names. See docs/live-e2e.md.
#
# Required for Conductor itself:
#   CONDUCTOR_BASE_URL
#   CONDUCTOR_AUTH_MODE
#   CONDUCTOR_INTERNAL_TOKEN
#
# Required for Agents Gateway:
#   CONDUCTOR_AGENTS_GATEWAY_URL
#   CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE
#   CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN
#
# Required for Skills Gateway:
#   CONDUCTOR_SKILLS_GATEWAY_URL
#   CONDUCTOR_SKILLS_GATEWAY_AUTH_MODE
#   CONDUCTOR_SKILLS_GATEWAY_INTERNAL_TOKEN
#
# Required for MCP Gateway (this milestone's core addition):
#   CONDUCTOR_MCP_GATEWAY_URL
#   CONDUCTOR_MCP_GATEWAY_AUTH_MODE
#   CONDUCTOR_MCP_GATEWAY_INTERNAL_TOKEN
#
# Optional wiki-mcp:
#   CONDUCTOR_WIKI_MCP_URL
#   CONDUCTOR_WIKI_MCP_AUTH_MODE
#   CONDUCTOR_WIKI_MCP_INTERNAL_TOKEN

set -euo pipefail

# ── Required environment ────────────────────────────────────────────────
REQUIRED_ENV=(
    CONDUCTOR_BASE_URL
    CONDUCTOR_AUTH_MODE
    CONDUCTOR_INTERNAL_TOKEN
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
REQUIRED_MCP_ENV=(
    CONDUCTOR_MCP_GATEWAY_URL
    CONDUCTOR_MCP_GATEWAY_AUTH_MODE
    CONDUCTOR_MCP_GATEWAY_INTERNAL_TOKEN
)
OPTIONAL_WIKI_ENV=(
    CONDUCTOR_WIKI_MCP_URL
    CONDUCTOR_WIKI_MCP_AUTH_MODE
    CONDUCTOR_WIKI_MCP_INTERNAL_TOKEN
)

MISSING=()
for v in "${REQUIRED_ENV[@]}" "${REQUIRED_AGENTS_ENV[@]}" \
         "${REQUIRED_SKILLS_ENV[@]}" "${REQUIRED_MCP_ENV[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        MISSING+=("$v")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "LIVE E2E BLOCKED: missing ${MISSING[*]}"
    echo ""
    echo "Set these variables and re-run."
    echo ""
    echo "Optional (wiki-mcp):"
    for v in "${OPTIONAL_WIKI_ENV[@]}"; do
        echo "  export $v=..."
    done
    exit 2
fi

# ── Derived state ────────────────────────────────────────────────────────
BASE="${CONDUCTOR_BASE_URL%/}"
AUTH_HEADERS=()
case "${CONDUCTOR_AUTH_MODE}" in
    internal-only|cloudflare-access)
        AUTH_HEADERS+=("-H" "X-Auth-Internal-Token: ${CONDUCTOR_INTERNAL_TOKEN}")
        ;;
    dev-none) : ;;
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

# fetch_to <out-file> <method> <url> [payload]
fetch_to() {
    local out="$1"; shift
    local method="$1"; shift
    local url="$1"; shift
    local status
    # Defensive: never hang a live E2E forever. 30s per request.
    if [[ "$method" == "GET" ]]; then
        status=$(curl -s --max-time 30 -o "$out" -w "%{http_code}" "${AUTH_HEADERS[@]}" "$url" 2>/dev/null || echo "000")
    else
        local payload="${1:-}"
        local accept="application/json"
        # MCP JSON-RPC endpoints prefer the SSE-capable Accept header.
        if [[ "$url" == */mcp* ]]; then
            accept="application/json, text/event-stream"
        fi
        status=$(curl -s --max-time 30 -o "$out" -w "%{http_code}" "${AUTH_HEADERS[@]}" -X "$method" "$url" \
            -H "Content-Type: application/json" -H "Accept: $accept" -d "$payload" 2>/dev/null || echo "000")
    fi
    echo "$status"
}

# fetch_mcp_call: performs a single MCP JSON-RPC call over the streamable
# HTTP transport. Does the initialize handshake to obtain a session id,
# then carries that id on the actual call. The session negotiation is
# cached in $MCP_SESSION_ID so the second and third calls reuse it.
MCP_PATH=""          # set on first call
MCP_SESSION_ID=""    # set on first call
fetch_mcp_call() {
    local out="$1"; shift
    local rpc_id="$1"; shift
    local method="$1"; shift
    local params="$1"; shift   # JSON-encoded params object (without outer braces)

    # Build URLs. /mcp is the configured mcp_path; FastMCP re-prefixes via
    # the sub-app's own router, so the JSON-RPC entry becomes /mcp/mcp.
    if [[ -z "$MCP_PATH" ]]; then
        MCP_PATH="${BASE}/mcp/mcp"
    fi

    # Initialize once (getting the session id)
    if [[ -z "$MCP_SESSION_ID" ]]; then
        local init_payload
        init_payload=$(python3 -c "
import json
print(json.dumps({
    'jsonrpc': '2.0', 'id': 0,
    'method': 'initialize',
    'params': {
        'protocolVersion': '2024-11-05',
        'capabilities': {},
        'clientInfo': {'name': 'conductor-live-e2e', 'version': '0.1'},
    },
}))
")
        local init_status
        init_status=$(curl -s --max-time 30 -o /tmp/live_hub_mcp_init.out -w "%{http_code}" \
            "${AUTH_HEADERS[@]}" -X POST "${MCP_PATH}" \
            -H "Content-Type: application/json" \
            -H "Accept: application/json, text/event-stream" \
            -d "${init_payload}" -D /tmp/live_hub_mcp_init.hdr 2>/dev/null || echo "000")
        if [[ "$init_status" != "200" ]]; then
            echo "$init_status"
            return
        fi
        MCP_SESSION_ID=$(awk 'tolower($1)=="mcp-session-id:" {gsub(/\r/,""); print $2}' /tmp/live_hub_mcp_init.hdr | head -1)
        if [[ -z "$MCP_SESSION_ID" ]]; then
            echo "$init_status"
            return
        fi
    fi

    # Issue the actual call with the session id
    local payload
    payload=$(python3 -c "
import json
print(json.dumps({
    'jsonrpc': '2.0', 'id': ${rpc_id},
    'method': '${method}',
    'params': {${params}},
}))
")
    local status
    status=$(curl -s --max-time 30 -o "${out}" -w "%{http_code}" \
        "${AUTH_HEADERS[@]}" -X POST "${MCP_PATH}" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "Mcp-Session-Id: ${MCP_SESSION_ID}" \
        -d "${payload}" 2>/dev/null || echo "000")
    echo "$status"
}

# mcp_extract_inner $out_file $py_expr
# Parses SSE-formatted body (lines `data: {...json...}`) and runs a small
# python snippet against the inner JSON-RPC result.content[0].text.
mcp_extract_inner() {
    local out="$1"; shift
    local expr="$1"; shift
    python3 -c "
import json,re,sys
raw=open('${out}').read()
m=re.search(r'^data: (.+)$', raw, re.MULTILINE)
data=json.loads(m.group(1)) if m else {}
content = data.get('result', {}).get('content', [])
text = content[0].get('text', '{}') if content else '{}'
inner = json.loads(text)
inner2 = inner.get('result', inner)
${expr}
"
}

echo "=== Conductor Gateway Hub Live E2E ==="
echo ""
echo "Conductor:       ${BASE} (auth=${CONDUCTOR_AUTH_MODE})"
echo "Agents Gateway:  ${CONDUCTOR_AGENTS_GATEWAY_URL} (auth=${CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE})"
echo "Skills Gateway:  ${CONDUCTOR_SKILLS_GATEWAY_URL} (auth=${CONDUCTOR_SKILLS_GATEWAY_AUTH_MODE})"
echo "MCP Gateway:     ${CONDUCTOR_MCP_GATEWAY_URL} (auth=${CONDUCTOR_MCP_GATEWAY_AUTH_MODE})"
echo "wiki-mcp:        ${CONDUCTOR_WIKI_MCP_URL:-<not configured>}"
echo ""

# ── Step 1: Health ───────────────────────────────────────────────────────
echo "--- Health ---"
RESP=$(fetch_to /tmp/live_hub_health.json GET "${BASE}/health")
check "health endpoint reachable" "200" "$RESP"
if [[ "$RESP" != "200" ]]; then
    echo "LIVE E2E FAILED: Conductor unreachable at ${BASE}/health"
    exit 1
fi
STATUS=$(python3 -c "import json; print(json.load(open('/tmp/live_hub_health.json'))['status'])")
check "health status ok" "ok" "$STATUS"

# ── Step 2: Version ──────────────────────────────────────────────────────
echo "--- Version ---"
RESP=$(fetch_to /tmp/live_hub_version.json GET "${BASE}/version")
check "version endpoint reachable" "200" "$RESP"

# ── Step 3: List gateways ────────────────────────────────────────────────
echo "--- List Gateways ---"
RESP=$(fetch_to /tmp/live_hub_gateways.json GET "${BASE}/gateways")
check "GET /gateways returns 200" "200" "$RESP"
if [[ "$RESP" == "200" ]]; then
    GW_COUNT=$(python3 -c "import json; print(json.load(open('/tmp/live_hub_gateways.json'))['count'])")
    echo "  gateway_count=${GW_COUNT}"
    check "gateway_count >= 4" "True" "$(python3 -c "print(True if int('${GW_COUNT}') >= 4 else False)")"
fi

# ── Step 4: Check all gateways ──────────────────────────────────────────
echo "--- Check All Gateways ---"
RESP=$(fetch_to /tmp/live_hub_checkall.json POST "${BASE}/gateways/check-all")
check "POST /gateways/check-all returns 200" "200" "$RESP"
if [[ "$RESP" == "200" ]]; then
    python3 -c "
import json
data = json.load(open('/tmp/live_hub_checkall.json'))
for s in data['statuses']:
    print(f\"  {s['id']:10s} {s['status']:15s} latency={s.get('latency_ms')}\")
"
fi

# ── Steps 5/6/7: Verify Agents / Skills / MCP all configured ─────────────
verify_configured() {
    local kind="$1"
    python3 -c "
import json, sys
data = json.load(open('/tmp/live_hub_checkall.json'))
for s in data['statuses']:
    if s['id'] == '${kind}' and s.get('configured'):
        print('yes'); sys.exit(0)
print('no'); sys.exit(0)
"
}

echo "--- Verify Agents Gateway configured ---"
AGENTS_CFG="$(verify_configured agents)"
check "agents gateway configured" "yes" "$AGENTS_CFG"

echo "--- Verify Skills Gateway configured ---"
SKILLS_CFG="$(verify_configured skills)"
check "skills gateway configured" "yes" "$SKILLS_CFG"

echo "--- Verify MCP Gateway configured ---"
MCP_CFG="$(verify_configured mcp)"
check "mcp gateway configured" "yes" "$MCP_CFG"

# ── Step 8: List capabilities ───────────────────────────────────────────
echo "--- List Capabilities ---"
RESP=$(fetch_to /tmp/live_hub_caps.json GET "${BASE}/capabilities")
check "GET /capabilities returns 200" "200" "$RESP"

# ── Step 9: Verify capability strings present ───────────────────────────
echo "--- Verify Capabilities ---"
HAS_EXEC_CREATE=$(python3 -c "
import json, sys
data = json.load(open('/tmp/live_hub_caps.json'))
names = {c['capability'] for c in data['capabilities']}
print('yes' if 'execution.task.create' in names else 'no')
")
check "execution.task.create present" "yes" "$HAS_EXEC_CREATE"

HAS_SKILLS_VALIDATE=$(python3 -c "
import json, sys
data = json.load(open('/tmp/live_hub_caps.json'))
names = {c['capability'] for c in data['capabilities']}
print('yes' if 'skills.validate' in names else 'no')
")
check "skills.validate present" "yes" "$HAS_SKILLS_VALIDATE"

HAS_TOOLS_LIST=$(python3 -c "
import json, sys
data = json.load(open('/tmp/live_hub_caps.json'))
names = {c['capability'] for c in data['capabilities']}
print('yes' if 'tools.list' in names else 'no')
")
check "tools.list present" "yes" "$HAS_TOOLS_LIST"

# ── Step 10: Create objective ────────────────────────────────────────────
echo "--- Create Objective ---"
OBJ_PAYLOAD='{"title":"hub-live-e2e","description":"Live Gateway Hub verification","priority":"normal"}'
RESP=$(fetch_to /tmp/live_hub_obj.json POST "${BASE}/objectives" "$OBJ_PAYLOAD")
check "create objective returns 201" "201" "$RESP"
OBJ_ID=$(python3 -c "import json; print(json.load(open('/tmp/live_hub_obj.json'))['objective_id'])")
echo "  objective_id=${OBJ_ID}"

# ── Step 11: Create task with required capabilities ──────────────────────
echo "--- Create Task With Required Capabilities ---"
TASK_PAY=$(python3 -c "
import json
print(json.dumps({
    'title': 'hub live task',
    'brief': 'Probe capabilities',
    'required_skills': [],
    'metadata': {'required_capabilities': ['execution.task.create']},
}))
")
RESP=$(fetch_to /tmp/live_hub_task.json POST "${BASE}/objectives/${OBJ_ID}/tasks" "$TASK_PAY")
check "create task returns 201" "201" "$RESP"
TASK_ID=$(python3 -c "import json; print(json.load(open('/tmp/live_hub_task.json'))['id'])")
echo "  task_id=${TASK_ID}"

# ── Step 12: Validate capability availability ───────────────────────────
echo "--- Validate Capabilities ---"
RESP=$(fetch_to /tmp/live_hub_findcap.json GET "${BASE}/capabilities/execution.task.create")
CAND_COUNT=$(python3 -c "import json; print(json.load(open('/tmp/live_hub_findcap.json'))['count'])")
check "execution.task.create has >=1 candidate" "True" "$(python3 -c "print(True if int('${CAND_COUNT}') >= 1 else False)")"

# ── Step 13: Dispatch task ──────────────────────────────────────────────
echo "--- Dispatch Task ---"
RESP=$(fetch_to /tmp/live_hub_dispatch.json POST "${BASE}/tasks/${TASK_ID}/dispatch")
if [[ "$RESP" == "200" ]]; then
    check "dispatch returns 200" "200" "$RESP"
    AR_STATUS=$(python3 -c "
import json
d = json.load(open('/tmp/live_hub_dispatch.json'))
print(d.get('status') or (d.get('agent_run') or {}).get('status') or '-')
")
    echo "  agent_run_status=${AR_STATUS}"
else
    ERR=$(python3 -c "import json; print(json.load(open('/tmp/live_hub_dispatch.json')).get('error','-'))" 2>/dev/null || echo "?")
    echo "  WARN: dispatch returned ${RESP} (error=${ERR})"
    check "dispatch returned 200" "200" "$RESP"
fi

# ── Step 14: Reconcile ──────────────────────────────────────────────────
echo "--- Reconcile ---"
RESP=$(fetch_to /tmp/live_hub_reconcile.json POST "${BASE}/reconcile")
check "reconcile returns 200" "200" "$RESP"

# ── Step 15: Fetch timeline ──────────────────────────────────────────────
echo "--- Timeline ---"
RESP=$(fetch_to /tmp/live_hub_timeline.json GET "${BASE}/objectives/${OBJ_ID}/timeline")
check "timeline returns 200" "200" "$RESP"
if [[ "$RESP" == "200" ]]; then
    TLINE_COUNT=$(python3 -c "import json; print(json.load(open('/tmp/live_hub_timeline.json'))['count'])")
    echo "  timeline_count=${TLINE_COUNT}"
    check "timeline non-empty" "True" "$(python3 -c "print(True if int('${TLINE_COUNT}') > 0 else False)")"
fi

# ── Step 16: Confirm timeline includes gateway events ────────────────────
echo "--- Timeline Includes Gateway Events ---"
if [[ "${TLINE_COUNT:-0}" -gt 0 ]]; then
    python3 -c "
import json
data = json.load(open('/tmp/live_hub_timeline.json'))
types = [e['event_type'] for e in data['events']]
gw_types = [t for t in types if t.startswith('gateway.')]
print(f'  gateway event types in timeline: {len(gw_types)}')
for t in sorted(set(gw_types)):
    print(f'    {t}')
"
fi

# ── Step 17: Exercise MCP tools ─────────────────────────────────────────
echo "--- MCP initialize + tools/list ---"
RESP=$(fetch_mcp_call /tmp/live_hub_mcp_listgw.json 1 \
    "tools/call" '"name": "conductor_list_gateways", "arguments": {}')
check "MCP conductor_list_gateways returns 200" "200" "$RESP"
if [[ "$RESP" == "200" ]]; then
    MCP_GW_COUNT=$(mcp_extract_inner /tmp/live_hub_mcp_listgw.json "print(inner2.get('count', '-'))")
    check "MCP gateway count >= 4" "True" "$(python3 -c "print(True if int('${MCP_GW_COUNT}') >= 4 else False)")"
fi

echo "--- MCP conductor_check_all_gateways ---"
RESP=$(fetch_mcp_call /tmp/live_hub_mcp_checkall.json 2 \
    "tools/call" '"name": "conductor_check_all_gateways", "arguments": {}')
check "MCP conductor_check_all_gateways returns 200" "200" "$RESP"

echo "--- MCP conductor_get_timeline ---"
TLINE_PARAMS='"name": "conductor_get_timeline", "arguments": {"objective_id": "'"$OBJ_ID"'"}'
RESP=$(fetch_mcp_call /tmp/live_hub_mcp_timeline.json 3 \
    "tools/call" "$TLINE_PARAMS")
check "MCP conductor_get_timeline returns 200" "200" "$RESP"

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "=== Results ==="
echo "Passed: ${PASS}"
echo "Failed: ${FAIL}"

if [[ ${FAIL} -eq 0 ]]; then
    echo "LIVE E2E PASSED"
    exit 0
fi

echo "LIVE E2E FAILED: see transcript above for exact failing step"
exit 1
