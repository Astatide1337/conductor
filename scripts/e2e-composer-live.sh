#!/usr/bin/env bash
# Live Composer E2E — spec-to-verified-execution with real Composer LLM,
# real Agents Gateway, real harness sessions.
#
# Proves 12 things:
#   1. real repository accessible to Agents Gateway
#   2. real Composer LLM used (provider != fake)
#   3. at least two real harness tasks
#   4. unique task worktree paths and branches
#   5. source files actually changed in the FINAL integration checkout
#   6. required task verification records passed via real GW endpoints
#   7. integration task completed
#   8. integration verification passed via real GW endpoint
#   9. final branch and commit SHA recovered from real evidence
#  10. HTML and JSON reports generated
#  11. reports contain verification and artifact evidence
#  12. objective completed
#
# Initial repo contains ONLY add(a,b) — multiply/divide are produced by the Composer pipeline.
#
# Final-checkout flow:
#   - clone the repository into a verification directory
#   - fetch the final integration branch
#   - checkout that branch
#   - verify HEAD equals the reported final commit SHA
#   - import multiply and divide from that checkout
#   - run uv run pytest -q in that checkout
#   - keep the checkout until all assertions finish
set -euo pipefail

# ── Required environment ───────────────────────────────────────────────────
REQUIRED_ENV=(
    CONDUCTOR_BASE_URL CONDUCTOR_AUTH_MODE CONDUCTOR_INTERNAL_TOKEN
    CONDUCTOR_COMPOSER_LLM_BASE_URL CONDUCTOR_COMPOSER_LLM_API_KEY CONDUCTOR_COMPOSER_LLM_MODEL
    CONDUCTOR_AGENTS_GATEWAY_URL CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN
)

MISSING=()
for v in "${REQUIRED_ENV[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        MISSING+=("$v")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "COMPOSER LIVE E2E BLOCKED: missing ${MISSING[*]}"
    echo ""
    echo "Set these variables and re-run:"
    for v in "${MISSING[@]}"; do
        echo "  export $v=..."
    done
    exit 2
fi

# ── Require an Agents-Gateway-accessible repository ──────────────────────
# Use file:// only when COMPOSER_LIVE_SHARED_VOLUME=true.
# Otherwise require COMPOSER_LIVE_REPO_URL.
REPO_URL="${COMPOSER_LIVE_REPO_URL:-}"
if [[ -z "${REPO_URL}" ]]; then
    if [[ "${COMPOSER_LIVE_SHARED_VOLUME:-}" == "true" ]]; then
        REPO_URL="file://${COMPOSER_LIVE_REPO_DIR:-/tmp/composer-live-repo}"
    else
        echo "COMPOSER LIVE E2E BLOCKED: missing COMPOSER_LIVE_REPO_URL"
        exit 2
    fi
fi

# ── Prove the real LLM client is active ───────────────────────────────────
# Require production environment and test_mode=false for the live test.
if [[ "${CONDUCTOR_ENVIRONMENT:-}" != "production" ]]; then
    echo "COMPOSER LIVE E2E BLOCKED: CONDUCTOR_ENVIRONMENT must be 'production' (got '${CONDUCTOR_ENVIRONMENT:-}')"
    exit 2
fi
if [[ "${CONDUCTOR_COMPOSER__TEST_MODE:-${CONDUCTOR_COMPOSER_TEST_MODE:-}}" == "true" ]]; then
    echo "COMPOSER LIVE E2E BLOCKED: CONDUCTOR_COMPOSER__TEST_MODE must be 'false' for live E2E"
    exit 2
fi

BASE="${CONDUCTOR_BASE_URL}"
AUTH_HEADER="X-Auth-Internal-Token: ${CONDUCTOR_INTERNAL_TOKEN}"
GW_BASE="${CONDUCTOR_AGENTS_GATEWAY_URL}"
GW_AUTH="X-Auth-Internal-Token: ${CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN}"
TIMEOUT_SEC="${COMPOSER_LIVE_TIMEOUT_SEC:-600}"
PASS=0
FAIL=0
STAGE=""
VERIFY_DIR=""

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
        echo "  FAIL: ${label} (expected pattern '${pattern}')"
        FAIL=$((FAIL + 1))
    fi
}

cleanup() {
    if [[ -n "${VERIFY_DIR}" && -d "${VERIFY_DIR}" ]]; then
        rm -rf "${VERIFY_DIR}"
    fi
}
trap cleanup EXIT

poll_until() {
    local desc="$1" max_sec="$2" url="$3" extract_py="$4" expected="$5"
    STAGE="${desc}"
    local waited=0
    local R val
    while [[ $waited -lt $max_sec ]]; do
        R=$(curl -sf -H "${AUTH_HEADER}" "${url}" 2>/dev/null || echo '{"error":"curl failed"}')
        val=$(echo "$R" | python3 -c "${extract_py}" 2>/dev/null || echo '')
        if echo "$val" | grep -q "${expected}"; then
            echo "  OK: ${desc} (after ${waited}s, value: ${val})"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    echo "  TIMEOUT: ${desc} after ${waited}s"
    echo "  Last response excerpt: $(echo "$R" | python3 -c "import sys; print(sys.stdin.read()[:300])" 2>/dev/null || echo 'parse error')"
    echo ""
    echo "COMPOSER LIVE E2E TIMED OUT: ${STAGE}"
    exit 1
}

echo "=== Composer Live E2E ==="
echo ""
echo "Conductor: ${BASE}"
echo "LLM model: ${CONDUCTOR_COMPOSER_LLM_MODEL}"
echo "Agents Gateway: ${GW_BASE}"
echo "Timeout: ${TIMEOUT_SEC}s"
echo ""

# ── 0. Setup repo (local test repo if file://, otherwise use provided URL) ─
STAGE="setup repo"
echo "--- Setup Repository (add-only) ---"
REPO_DIR="${COMPOSER_LIVE_REPO_DIR:-/tmp/composer-live-repo-$(date +%s)}"
if [[ "${REPO_URL}" == file://* ]]; then
    REPO_DIR="${REPO_URL#file://}"
    if [[ ! -d "${REPO_DIR}/.git" ]]; then
        mkdir -p "${REPO_DIR}/calculator"
        cd "${REPO_DIR}"
        git init
        git config user.email "composer@e2e.test"
        git config user.name "Composer Live E2E"
        echo '# Disposable live test repo' > README.md
        cat > calculator/__init__.py <<'EOF'
"""Simple calculator package for E2E testing."""

def add(a: int, b: int) -> int:
    return a + b
EOF
        cat > calculator/test_calculator.py <<'EOF'
from calculator import add

def test_add():
    assert add(2, 3) == 5
    assert add(0, 0) == 0
EOF
        cat > pyproject.toml <<'EOF'
[project]
name = "calculator"
version = "0.1.0"

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
minversion = "7.0"
testpaths = ["calculator"]
EOF
        git add .
        git commit -m "Initial: add-only calculator"
        cd -
    fi
fi
echo "  Repo URL: ${REPO_URL}"
echo "  Repo dir: ${REPO_DIR}"

if [[ "${REPO_URL}" == file://* ]]; then
    INITIAL_ADD=$(cd "${REPO_DIR}" && python3 -c "from calculator import add; print(add(1,2))" 2>/dev/null || echo "err")
    check "initial add works" "3" "${INITIAL_ADD}"
    INITIAL_MULTI=$(cd "${REPO_DIR}" && python3 -c "from calculator import multiply; print('yes')" 2>/dev/null || echo "no")
    check "no multiply yet" "no" "${INITIAL_MULTI}"
fi

# ── 1. Health ─────────────────────────────────────────────────────────────
STAGE="health"
echo "--- Health ---"
R=$(curl -sf "${BASE}/health")
check "health ok" "ok" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"

# ── 1b. Verify real LLM provider is active ────────────────────────────────
STAGE="llm provider check"
echo "--- LLM Provider ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/health")
LLM_PROVIDER=$(echo "$R" | python3 -c "
import sys, json
d = json.load(sys.stdin)
# composer_llm_provider may be at top level or in composer section
p = d.get('composer_llm_provider', '') or d.get('composer', {}).get('llm_provider', '')
print(p)
" 2>/dev/null || echo "")
check_contains "llm provider is http (not fake)" "^http$" "${LLM_PROVIDER}"
LLM_MODEL_DIAG=$(echo "$R" | python3 -c "
import sys, json
d = json.load(sys.stdin)
m = d.get('composer_llm_model', '') or d.get('composer', {}).get('llm_model', '')
print(m)
" 2>/dev/null || echo "")
check_contains "llm model reported" ".\{2\}" "${LLM_MODEL_DIAG}"

# ── 2. Submit calculator spec — add multiply and divide ───────────────────
STAGE="submit spec"
echo "--- Submit Spec ---"

SPEC_TEXT='Extend the calculator package with multiply and divide functions.
Requirements:
- Add multiply(a,b) returning a*b.
- Add divide(a,b) returning a/b; raise ValueError for b=0.
- Use the repository and base branch provided.
- Include pytest tests in calculator/test_calculator.py.
- Run uv run pytest -q in the project root.
- Produce an integration branch with all changes.'

BRANCH_NAME="${COMPOSER_LIVE_REPO_BRANCH:-main}"
if [[ "${REPO_URL}" == file://* && -d "${REPO_DIR}/.git" ]]; then
    BRANCH_NAME=$(cd "${REPO_DIR}" && git rev-parse --abbrev-ref HEAD)
fi

R=$(curl -sf -X POST "${BASE}/composer/objectives" \
    -H "Content-Type: application/json" \
    -H "${AUTH_HEADER}" \
    -d "$(python3 -c "
import json
print(json.dumps({
    'title': 'Live E2E Calculator Extension',
    'spec': '''${SPEC_TEXT}''',
    'repository': {'url': '${REPO_URL}', 'base_branch': '${BRANCH_NAME}'},
    'auto_start': True,
}))
")")
OBJ_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['objective_id'])")
SPEC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['composer_spec_id'])")
check "spec submitted" "received" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"
echo "  objective_id=${OBJ_ID}  spec_id=${SPEC_ID}"
echo "  repo_url=${REPO_URL}  branch=${BRANCH_NAME}"

# ── 3. Repository preserved ────────────────────────────────────────────────
echo "--- Repository Preservation ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/spec")
PERSISTED_URL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('repository_url',''))")
PERSISTED_BR=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_branch',''))")
check_contains "repo url persisted" "${REPO_URL}" "${PERSISTED_URL}"
check "base branch persisted" "${BRANCH_NAME}" "${PERSISTED_BR}"

# ── 4. Spec advances from received through intermediate states ────────────
poll_until "spec advanced" 120 "${BASE}/composer/objectives/${OBJ_ID}/spec" \
    "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" \
    "normalized\|planning\|planned\|executing\|integrating\|verifying\|completed"

# ── 5. Plan generated with at least 2 implementation tasks ────────────────
poll_until "plan generated" 120 "${BASE}/composer/objectives/${OBJ_ID}/plan" \
    "import sys,json; d=json.load(sys.stdin); imp=[t for t in d.get('plan_tasks',[]) if t.get('node_key')!='integration']; print(len(imp))" \
    "2\|3\|4\|5\|6"

# ── 6. At least 2 real harness tasks dispatched ────────────────────────────
poll_until "tasks dispatched" 180 "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
    "import sys,json; d=json.load(sys.stdin); dispatched=[t for t in d.get('tasks',[]) if t.get('status') in ('dispatching','running','completed','verifying')]; print(len(dispatched))" \
    "2\|3\|4\|5"

# ── 7. Unique task worktree paths and branches (assertion 4) ──────────────
echo "--- Distinct Worktrees ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/tasks")
GW_IDS=$(echo "$R" | python3 -c "
import sys,json
tasks = json.load(sys.stdin).get('tasks',[])
ids = [t.get('agents_gateway_task_id') for t in tasks if t.get('agents_gateway_task_id')]
print(len(set(ids)))
")
check_contains "at least 2 distinct gw task ids" "2\|3\|4\|5" "${GW_IDS}"

# Query actual worktree endpoints to prove unique paths and branches
WT_RESULT=$(echo "${R}" | python3 -c "
import sys, json, os, urllib.request
tasks = json.load(sys.stdin).get('tasks', [])
gw_base = os.environ.get('CONDUCTOR_AGENTS_GATEWAY_URL', 'http://localhost:8092')
gw_token = os.environ.get('CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN', '')
headers = {}
if gw_token:
    headers['X-Auth-Internal-Token'] = gw_token
paths = []
branches = []
for t in tasks:
    gw_id = t.get('agents_gateway_task_id', '')
    if not gw_id:
        continue
    try:
        req = urllib.request.Request(f'{gw_base}/tasks/{gw_id}/worktree', headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            paths.append(data.get('path', ''))
            branches.append(data.get('branch', ''))
    except Exception:
        pass
print(f\"paths={len(set(paths))} branches={len(set(branches))}\")
" 2>/dev/null || echo "paths=0 branches=0")
echo "  worktree uniqueness: ${WT_RESULT}"
check_contains "at least 2 distinct worktree paths" "paths=2\|paths=3\|paths=4\|paths=5" "${WT_RESULT}"
check_contains "at least 2 distinct branches" "branches=2\|branches=3\|branches=4\|branches=5" "${WT_RESULT}"

# ── 8. Implementation tasks complete with verification passed ──────────────
poll_until "implementation tasks completed" $((TIMEOUT_SEC - 200)) "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
    "import sys,json; tasks=json.load(sys.stdin).get('tasks',[]); ct=sum(1 for t in tasks if t.get('task_type')!='integration' and t.get('status')=='completed'); print(ct)" \
    "2\|3\|4\|5"

# ── 9. Query real verification endpoints for every implementation task ─────
STAGE="check per-task verification"
echo "--- Per-Task Verification Endpoints (real GW) ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/tasks")
VERIF_RESULT=$(echo "$R" | python3 -c "
import sys, json, os, urllib.request
tasks = json.load(sys.stdin).get('tasks', [])
gw_base = os.environ.get('CONDUCTOR_AGENTS_GATEWAY_URL', '')
gw_token = os.environ.get('CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN', '')
headers = {}
if gw_token:
    headers['X-Auth-Internal-Token'] = gw_token

passed_count = 0
required_cmd_failures = 0
integration_passed = False

for t in tasks:
    gw_id = t.get('agents_gateway_task_id', '')
    if not gw_id:
        continue
    is_integration = (t.get('task_type') == 'integration' or t.get('node_key') == 'integration')
    # Resolve real agent_run_id and call verification endpoint
    try:
        # First get the task session to find agent_run_id
        req = urllib.request.Request(f'{gw_base}/tasks/{gw_id}/session', headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            session_data = json.loads(resp.read())
            agent_run_id = session_data.get('agent_run_id', '')
            if not agent_run_id:
                agent_run_id = gw_id  # fallback
        req = urllib.request.Request(f'{gw_base}/agent-runs/{agent_run_id}/verification', headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            verif = json.loads(resp.read())
        status = verif.get('status', '')
        commands = verif.get('commands', [])
        if status == 'passed':
            passed_count += 1
        # Check every required command exists and passed
        for cmd in commands:
            if isinstance(cmd, dict) and cmd.get('required', False):
                if not cmd.get('passed', False):
                    required_cmd_failures += 1
        if is_integration and status == 'passed':
            integration_passed = True
    except Exception as e:
        pass

print(f'passed={passed_count} failures={required_cmd_failures} integration_passed={integration_passed}')
" 2>/dev/null || echo "passed=0 failures=0 integration_passed=False")
echo "  verification: ${VERIF_RESULT}"
check_contains "at least 2 tasks with verification passed via real GW" "passed=2\|passed=3\|passed=4\|passed=5" "${VERIF_RESULT}"
check_contains "0 required command failures" "^failures=0$" "${VERIF_RESULT}"

# ── 10. Integration task completed ────────────────────────────────────────
poll_until "integration completed" $((TIMEOUT_SEC - 100)) "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
    "import sys,json; tasks=json.load(sys.stdin).get('tasks',[]); it=[t for t in tasks if t.get('task_type')=='integration' or t.get('node_key')=='integration']; print(it[0].get('status','') if it else 'none')" \
    "completed"

# ── 11. Integration verification passed via real GW endpoint ───────────────
STAGE="check integration verification"
echo "--- Integration Verification (real GW) ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/tasks")
INTEG_VERIF=$(echo "$R" | python3 -c "
import sys, json, os, urllib.request
tasks = json.load(sys.stdin).get('tasks', [])
gw_base = os.environ.get('CONDUCTOR_AGENTS_GATEWAY_URL', '')
gw_token = os.environ.get('CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN', '')
headers = {}
if gw_token:
    headers['X-Auth-Internal-Token'] = gw_token
it = [t for t in tasks if t.get('node_key')=='integration']
if not it:
    print('none')
else:
    gw_id = it[0].get('agents_gateway_task_id', '')
    try:
        req = urllib.request.Request(f'{gw_base}/tasks/{gw_id}/session', headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            session_data = json.loads(resp.read())
            agent_run_id = session_data.get('agent_run_id', '') or gw_id
        req = urllib.request.Request(f'{gw_base}/agent-runs/{agent_run_id}/verification', headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            verif = json.loads(resp.read())
        print(verif.get('status', ''))
    except Exception as e:
        print(f'error: {e}')
" 2>/dev/null || echo "error")
check "integration verification passed (real GW)" "passed" "${INTEG_VERIF}"

# ── 12. Final branch and commit SHA recovered from real evidence ───────────
STAGE="check branch/commit"
echo "--- Final Branch & Commit ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/report")
FINAL_BR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('final_branch',''))")
FINAL_SH=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('final_commit_sha',''))")
check_contains "final branch present" ".\{3\}" "${FINAL_BR}"
check_contains "final commit sha present" ".\{3\}" "${FINAL_SH}"
echo "  branch=${FINAL_BR}  commit=${FINAL_SH}"

# ── 13. Clone/fetch the final integration branch (proper final-checkout flow) ──
STAGE="final checkout verification"
echo "--- Final Checkout (clone, fetch, checkout, verify HEAD) ---"
# Use a unique verification directory and keep it until all assertions finish
VERIFY_DIR="/tmp/composer-verify-$(date +%s)"
# Clone the repository (from the original URL, not a worktree copy)
git clone "${REPO_URL}" "${VERIFY_DIR}" 2>/dev/null
if [[ ! -d "${VERIFY_DIR}/.git" ]]; then
    echo "  FAIL: clone failed for ${REPO_URL}"
    FAIL=$((FAIL + 1))
else
    cd "${VERIFY_DIR}"
    # Fetch the final integration branch
    git fetch origin "${FINAL_BR}" 2>/dev/null || git fetch origin 2>/dev/null || true
    # Checkout that branch
    git checkout "${FINAL_BR}" 2>/dev/null || git checkout "origin/${FINAL_BR}" 2>/dev/null || true
    # Verify HEAD equals the reported final commit SHA
    HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
    if [[ -n "${HEAD_SHA}" && -n "${FINAL_SH}" ]]; then
        if [[ "${HEAD_SHA}" == "${FINAL_SH}" || "${HEAD_SHA}" == "${FINAL_SH}"* ]]; then
            echo "  PASS: final checkout HEAD matches reported commit SHA"
            PASS=$((PASS + 1))
        else
            echo "  FAIL: HEAD (${HEAD_SHA}) != reported SHA (${FINAL_SH})"
            FAIL=$((FAIL + 1))
        fi
    else
        echo "  FAIL: could not resolve HEAD or FINAL_SH"
        FAIL=$((FAIL + 1))
    fi
    cd - >/dev/null
fi

# ── 14. Import multiply and divide from the final checkout ─────────────────
STAGE="import from final checkout"
echo "--- Import multiply & divide from final checkout ---"
if [[ -d "${VERIFY_DIR}" ]]; then
    HAS_MULTIPLY=$(cd "${VERIFY_DIR}" && python3 -c "from calculator import multiply; print('yes')" 2>/dev/null || echo "no")
    check "multiply function added in final checkout (strict)" "yes" "${HAS_MULTIPLY}"
    HAS_DIVIDE=$(cd "${VERIFY_DIR}" && python3 -c "from calculator import divide; print('yes')" 2>/dev/null || echo "no")
    check "divide function added in final checkout (strict)" "yes" "${HAS_DIVIDE}"
else
    echo "  FAIL: verify directory missing"
    FAIL=$((FAIL + 1))
fi

# ── 15. Run pytest against the FINAL checkout (not the base checkout) ─────
STAGE="run pytest on final checkout"
echo "--- Run pytest in final checkout ---"
if [[ -d "${VERIFY_DIR}" ]] && cd "${VERIFY_DIR}" && uv run pytest -q 2>/dev/null; then
    echo "  PASS: pytest passed against final checkout"
    PASS=$((PASS + 1))
else
    echo "  FAIL: pytest failed against final checkout"
    FAIL=$((FAIL + 1))
fi
cd - >/dev/null 2>/dev/null || true

# ── 16. HTML and JSON reports exist ─────────────────────────────────────────
STAGE="check reports"
echo "--- Reports ---"
poll_until "report exists" 120 "${BASE}/composer/objectives/${OBJ_ID}/report" \
    "import sys,json; d=json.load(sys.stdin); print(d.get('json_artifact_ref',''))" \
    "[a-z]"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/report")
HTML=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('html_artifact_ref',''))")
JSON=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('json_artifact_ref',''))")
check_contains "HTML report ref present" ".\{3\}" "${HTML}"
check_contains "JSON report ref present" ".\{3\}" "${JSON}"
echo "  html=${HTML}"
echo "  json=${JSON}"

# ── 17. Reports contain downstream artifact references ────────────────────
STAGE="check report downstream artifacts"
echo "--- Downstream Artifact References ---"
if [[ -f "${JSON}" ]]; then
    HAS_VERIF=$(python3 -c "import json; d=json.load(open('${JSON}')); v=d.get('verification',[]); print(len(v))" 2>/dev/null || echo "0")
    check_contains "report has verification rows" "1\|2\|3\|4\|5" "${HAS_VERIF}"
    HAS_TASKS=$(python3 -c "import json; d=json.load(open('${JSON}')); t=d.get('task_graph',[]); print(len(t))" 2>/dev/null || echo "0")
    check_contains "report has task_graph entries" "1\|2\|3\|4\|5" "${HAS_TASKS}"
    HAS_ARTIFACTS=$(python3 -c "import json; d=json.load(open('${JSON}')); a=d.get('downstream_artifacts',[]); print(len(a))" 2>/dev/null || echo "0")
    check_contains "report has downstream_artifacts entries" "1\|2\|3\|4\|5" "${HAS_ARTIFACTS}"
else
    echo "  FAIL: JSON report file not found"
    FAIL=$((FAIL + 1))
fi

# ── 18. Objective completed ───────────────────────────────────────────────
STAGE="check completion"
echo "--- Completion ---"
poll_until "objective completed" 60 "${BASE}/composer/objectives/${OBJ_ID}" \
    "import sys,json; d=json.load(sys.stdin); spec=d.get('composer_spec',{}); print(spec.get('status',''))" \
    "completed"

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
