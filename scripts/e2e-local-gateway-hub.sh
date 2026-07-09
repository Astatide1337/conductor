#!/usr/bin/env bash
# Local Gateway Hub E2E — fully offline / mocked.
#
# Verifies the full Gateway Hub surface against a freshly-started Conductor
# in dev-none auth mode. Does NOT talk to any real downstream gateway —
# all health probes for the localhost-default agents/skills gateways will
# return unhealthy (no real servers) but the route plumbing, capability
# catalog, dispatch + reconcile, timeline, and MCP surface are all
# exercised truthfully.
#
# Run from the repo root after `docker compose up -d --build` (or any
# equivalent way of booting Conductor on localhost:8093). The script
# is safe to invoke directly because every failure produces a labelled
# CHECK line so debugging is straightforward.
#
# Expected final output:
#
#     Passed: N
#     Failed: 0
#     ✅ Gateway Hub local E2E passed
set -euo pipefail

HOST="${CONDUCTOR_HOST:-localhost:8093}"
BASE="http://${HOST}"
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

echo "=== Conductor Gateway Hub Local E2E ==="
echo ""

# 1. Health (public)
echo "--- Health ---"
R=$(curl -sf "${BASE}/health")
check "health ok" "ok" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"

# 2. Version (public)
echo "--- Version ---"
R=$(curl -sf "${BASE}/version")
check "version service" "astatide-conductor" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['service'])")"

# 3. List gateways
echo "--- List Gateways ---"
R=$(curl -sf "${BASE}/gateways")
GATEWAY_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])")
check "gateways count is 4" "4" "$GATEWAY_COUNT"
GATEWAY_IDS=$(echo "$R" | python3 -c "import json,sys; data=json.load(sys.stdin); print(' '.join(sorted(g['id'] for g in data['gateways'])))")
check "agents gateway present" "agents mcp skills wiki" "$GATEWAY_IDS"

# 4. Gateway status (lightweight)
echo "--- Gateways Status ---"
R=$(curl -sf "${BASE}/gateways/status")
WIKI_STATUS=$(echo "$R" | python3 -c "import json,sys; data=json.load(sys.stdin); print(next(g['status'] for g in data['gateways'] if g['id']=='wiki'))")
check "wiki not_configured" "not_configured" "$WIKI_STATUS"

# 5. Check all (live probe — mcp/wiki not_configured, agents/skills unhealthy because no real server)
echo "--- Check All Gateways ---"
R=$(curl -sf -X POST "${BASE}/gateways/check-all")
CHECK_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])")
check "check-all count is 4" "4" "$CHECK_COUNT"
# MCP gateway should be not_configured even after probe
MCP_STATUS=$(echo "$R" | python3 -c "import json,sys; data=json.load(sys.stdin); print(next(s['status'] for s in data['statuses'] if s['id']=='mcp'))")
check "mcp not_configured after probe" "not_configured" "$MCP_STATUS"

# 6. Get a single gateway
echo "--- Get Gateway ---"
R=$(curl -sf "${BASE}/gateways/agents")
check "get agents gateway kind" "agents" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['gateway']['kind'])")"

# 7. Check single gateway health
echo "--- Check Single Gateway ---"
R=$(curl -sf -X POST "${BASE}/gateways/mcp/check")
check "mcp check returns not_configured" "not_configured" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status']['status'])")"

# 8. List capabilities
echo "--- List Capabilities ---"
R=$(curl -sf "${BASE}/capabilities")
CAP_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])")
HAS_EXEC_CREATE=$(echo "$R" | python3 -c "
import json,sys
data=json.load(sys.stdin)
caps={c['capability'] for c in data['capabilities']}
print('yes' if 'execution.task.create' in caps else 'no')
")
check "execution.task.create in caps" "yes" "$HAS_EXEC_CREATE"

# 9. Find capability
echo "--- Find Capability ---"
R=$(curl -sf "${BASE}/capabilities/execution.task.create")
CAND_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])")
check "find execution.task.create has 1 candidate" "1" "$CAND_COUNT"

# 10. Create objective
echo "--- Create Objective ---"
R=$(curl -sf -X POST "${BASE}/objectives" -H "Content-Type: application/json" -d '{"title":"hub-e2e","description":"Gateway Hub local smoke"}')
OBJ_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective_id'])")
check "objective created" "created" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"
echo "  objective_id=${OBJ_ID}"

# 11. Create task with required capabilities
echo "--- Create Task With Required Capabilities ---"
TASK_PAY='{"title":"hub task","brief":"Probe capabilities","required_skills":[],"metadata":{"required_capabilities":["execution.task.create"]}}'
R=$(curl -sf -X POST "${BASE}/objectives/${OBJ_ID}/tasks" -H "Content-Type: application/json" -d "$TASK_PAY")
TASK_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "  task_id=${TASK_ID}"
check "task created" "created" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"

# 12. Dispatch (capabilities satisfied → should succeed)
echo "--- Dispatch Task ---"
R=$(curl -sf -X POST "${BASE}/tasks/${TASK_ID}/dispatch")
DISPATCH_STATUS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','-'))")
check "dispatch returns running or dispatched" "running" "$DISPATCH_STATUS"
echo "  agent_run_status=${DISPATCH_STATUS}"

# 13. Reconcile — just verify the call 200s and returns a numeric
#     reconciled count. The exact number depends on what else is in-flight
#     in the storage DB (e.g. leftover runs from earlier E2E scripts), so
#     asserting ==0 is unstable in a shared-DB context.
echo "--- Reconcile ---"
R=$(curl -sf -X POST "${BASE}/reconcile")
RECONCILED=$(echo "$R" | python3 -c "import sys,json; v=json.load(sys.stdin).get('reconciled',-1); print(v if isinstance(v,int) else -1)")
check "reconcile returns 200 + numeric reconciled count" "yes" "$( [ "$RECONCILED" -ge 0 ] && echo yes || echo no )"

# 14. Fetch timeline — should include objective.created, task.created, task.dispatch_requested etc.
echo "--- Timeline ---"
R=$(curl -sf "${BASE}/objectives/${OBJ_ID}/timeline")
TLINE_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])")
check "timeline non-empty" "True" "$(python3 -c "print(True if int('${TLINE_COUNT}') > 0 else False)")"
TLINE_TYPES=$(echo "$R" | python3 -c "
import json,sys
data=json.load(sys.stdin)
types=[e['event_type'] for e in data['events']]
print('|'.join(types))
")
echo "  timeline_types=${TLINE_TYPES}"
# Verify gateway.agents.dispatch appears in timeline (capability-targeted action)
HAS_GW_DISPATCH=$(python3 -c "
import json,sys
data=json.loads('''$TLINE_TYPES'''.replace('|',' ').split()[0])
print('yes' if 'gateway.agents.dispatch' in 'gateway.agents.dispatch' else 'no')
" 2>/dev/null || echo "yes")
# Simpler check — does the timeline contain the expected event type string?
TLINE_INCLUDES_GW_DISPATCH=$(echo "$TLINE_TYPES" | grep -c "gateway.agents.dispatch" || true)
check "timeline includes gateway.agents.dispatch" "1" "$TLINE_INCLUDES_GW_DISPATCH"

# 15. MCP handshake + tools/list includes all gateway hub tools
#     FastMCP streamable HTTP requires an explicit initialize handshake to
#     obtain a session id, then all subsequent calls must carry that id.
#     The path is /mcp/mcp (the mounted sub-app re-prefixes by its own
#     mcp_path, /mcp, so the full route doubles up).
echo "--- MCP tools/list ---"
MCP_PATH="${BASE}/mcp/mcp"

INIT_RESP=$(curl -s --max-time 30 -X POST "${MCP_PATH}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -D /tmp/mcp_hdrs.txt \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"0.1"}}}')
SESSION_ID=$(grep -i "mcp-session-id:" /tmp/mcp_hdrs.txt | head -1 | tr -d '\r' | awk '{print $2}')
if [[ -z "${SESSION_ID}" ]]; then
    echo "  FAIL: MCP initialize did not return mcp-session-id"
    FAIL=$((FAIL + 1))
    TOOL_NAMES=""
else
    echo "  PASS: MCP initialize session created"
    PASS=$((PASS + 1))
    R=$(curl -s --max-time 30 -X POST "${MCP_PATH}" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "Mcp-Session-Id: ${SESSION_ID}" \
        -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
    # The response is SSE-formatted: lines like `data: {...json...}`.
    TOOL_NAMES=$(echo "$R" | python3 -c "
import json,sys,re
raw=sys.stdin.read()
m=re.search(r'^data: (.+)$', raw, re.MULTILINE)
data=json.loads(m.group(1)) if m else {}
if 'result' in data and 'tools' in data['result']:
    print(' '.join(sorted(t['name'] for t in data['result']['tools'])))
else:
    print('')
")
    echo "  tools=${TOOL_NAMES}"
fi
EXPECTED_TOOLS=("conductor_list_gateways" "conductor_get_gateway_status" \
                 "conductor_check_gateway_health" "conductor_check_all_gateways" \
                 "conductor_list_capabilities" "conductor_find_capability" \
                 "conductor_get_timeline")
for t in "${EXPECTED_TOOLS[@]}"; do
    if echo " $TOOL_NAMES " | grep -q " $t "; then
        echo "  PASS: MCP tool $t registered"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: MCP tool $t missing"
        FAIL=$((FAIL + 1))
    fi
done

# 16. MCP call conductor_list_gateways
echo "--- MCP conductor_list_gateways ---"
R=$(curl -s --max-time 30 -X POST "${MCP_PATH}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: ${SESSION_ID}" \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"conductor_list_gateways","arguments":{}}}')
# SSE-wrapped result, top-level result.content[0].text is the JSON-encoded tool body.
GW_COUNT_VIA_MCP=$(echo "$R" | python3 -c "
import json,sys,re
raw=sys.stdin.read()
m=re.search(r'^data: (.+)$', raw, re.MULTILINE)
data=json.loads(m.group(1)) if m else {}
content = data.get('result', {}).get('content', [])
text = content[0].get('text', '{}') if content else '{}'
inner = json.loads(text)
inner2 = inner.get('result', inner)  # fastmcp double-wraps for non-object returns
print(inner2.get('count', '-'))
")
check "MCP conductor_list_gateways returned 4" "4" "$GW_COUNT_VIA_MCP"

# 17. MCP call conductor_get_timeline for the objective we created
echo "--- MCP conductor_get_timeline ---"
R=$(curl -s --max-time 30 -X POST "${MCP_PATH}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: ${SESSION_ID}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"conductor_get_timeline\",\"arguments\":{\"objective_id\":\"${OBJ_ID}\"}}}")
TLINE_MCP_COUNT=$(echo "$R" | python3 -c "
import json,sys,re
raw=sys.stdin.read()
m=re.search(r'^data: (.+)$', raw, re.MULTILINE)
data=json.loads(m.group(1)) if m else {}
content = data.get('result', {}).get('content', [])
text = content[0].get('text', '{}') if content else '{}'
inner = json.loads(text)
inner2 = inner.get('result', inner)
print(inner2.get('count', 0))
")
check "MCP get_timeline returned non-empty" "True" "$(python3 -c "print(True if int('${TLINE_MCP_COUNT}') > 0 else False)")"

# ── Summary ────────────────────────────────────────────────────────────
echo ""
echo "=== Results ==="
echo "Passed: ${PASS}"
echo "Failed: ${FAIL}"
echo ""

if [[ $FAIL -gt 0 ]]; then
    echo "❌ Gateway Hub local E2E failed"
    exit 1
fi

echo "✅ Gateway Hub local E2E passed"
exit 0
