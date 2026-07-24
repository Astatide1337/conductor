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
#   9. integration branch pushed to remote (auto_push required)
#  10. final branch and commit SHA recovered from real evidence
#  11. final commit SHA differs from starting baseline (work was actually done)
#  12. HTML and JSON report content fetched via HTTP (not local filesystem)
#  13. reports contain verification and downstream artifact evidence
#  14. objective completed
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
set -uo pipefail

# ── Failure functions + globals + trap MUST be defined before any
#     prerequisite check — otherwise _block / _fail produce
#     "command not found" and the script continues in an inconsistent
#     state.
STAGE="prerequisites"
_CANONICAL_PRINTED=false
PASS=0
FAIL=0
VERIFY_DIR=""
BASELINE_SETUP_DIR=""
BASELINE_BRANCH=""

_fail() {
    echo ""
    echo "COMPOSER LIVE E2E FAILED: ${STAGE:-unknown stage} — $1"
    _CANONICAL_PRINTED=true
    exit 1
}

_block() {
    echo ""
    echo "COMPOSER LIVE E2E BLOCKED: ${STAGE:-unknown stage} — $1"
    _CANONICAL_PRINTED=true
    exit 2
}

cleanup() {
    if [[ -n "${VERIFY_DIR}" && -d "${VERIFY_DIR}" ]]; then
        rm -rf "${VERIFY_DIR}"
    fi
    # Keep BASELINE_SETUP_DIR on failure for diagnosis; only remove on
    # script-completed-clean. We test the captured exit-status via
    # the E2E_CLEAN_OK flag set at the end of the script.
    if [[ -n "${BASELINE_SETUP_DIR}" && -d "${BASELINE_SETUP_DIR}" && "${E2E_CLEAN_OK:-0}" == "1" ]]; then
        rm -rf "${BASELINE_SETUP_DIR}"
    fi
    # Only delete the disposable baseline branch if the script
    # completed cleanly. Otherwise the integration branch still in
    # flight depends on this baseline and `git fetch composer-...-baseline-N`
    # in AGW workspace.fetch() will fail with
    # "couldn't find remote ref composer-live-baseline-...".
    if [[ "${E2E_CLEAN_OK:-0}" == "1" \
            && -n "${BASELINE_BRANCH:-}" \
            && "${REPO_URL:-}" != file://* \
            && -n "${REPO_URL:-}" ]]; then
        git push "${REPO_URL}" --delete "${BASELINE_BRANCH}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

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

# ── Required environment ───────────────────────────────────────────────────
REQUIRED_ENV=(
    CONDUCTOR_BASE_URL CONDUCTOR_AUTH_MODE CONDUCTOR_INTERNAL_TOKEN
    CONDUCTOR_COMPOSER_LLM_BASE_URL CONDUCTOR_COMPOSER_LLM_API_KEY CONDUCTOR_COMPOSER_LLM_MODEL
    CONDUCTOR_AGENTS_GATEWAY_URL CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN
    AGW_HARNESS__AUTO_PUSH
)

MISSING=()
for v in "${REQUIRED_ENV[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        MISSING+=("$v")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo ""
    echo "Set these variables and re-run:"
    for v in "${MISSING[@]}"; do
        echo "  export $v=..."
    done
    _block "missing ${MISSING[*]}"
fi

# ── Validate AGW_HARNESS__AUTO_PUSH is affirmatively true ──────────────────
case "${AGW_HARNESS__AUTO_PUSH,,}" in
    true|1|yes) ;;
    *)
        _block "AGW_HARNESS__AUTO_PUSH must be true/1/yes (got '${AGW_HARNESS__AUTO_PUSH}')"
        ;;
esac

# ── Require an Agents-Gateway-accessible repository ──────────────────────
REPO_URL="${COMPOSER_LIVE_REPO_URL:-}"
if [[ -z "${REPO_URL}" ]]; then
    if [[ "${COMPOSER_LIVE_SHARED_VOLUME:-}" == "true" ]]; then
        REPO_URL="file://${COMPOSER_LIVE_REPO_DIR:-/tmp/composer-live-repo}"
    else
        _block "missing COMPOSER_LIVE_REPO_URL"
    fi
fi

# ── Prove the real LLM client is active ───────────────────────────────────
if [[ "${CONDUCTOR_ENVIRONMENT:-}" != "production" ]]; then
    _block "CONDUCTOR_ENVIRONMENT must be 'production' (got '${CONDUCTOR_ENVIRONMENT:-}')"
fi
if [[ "${CONDUCTOR_COMPOSER__TEST_MODE:-${CONDUCTOR_COMPOSER_TEST_MODE:-}}" == "true" ]]; then
    _block "CONDUCTOR_COMPOSER__TEST_MODE must be 'false' for live E2E"
fi

BASE="${CONDUCTOR_BASE_URL}"
AUTH_HEADER="X-Auth-Internal-Token: ${CONDUCTOR_INTERNAL_TOKEN}"
GW_BASE="${CONDUCTOR_AGENTS_GATEWAY_URL}"
GW_AUTH="X-Auth-Internal-Token: ${CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN}"
TIMEOUT_SEC="${COMPOSER_LIVE_TIMEOUT_SEC:-600}"

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
    _fail "timeout — ${STAGE}"
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
dependencies = ["pytest>=7"]

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
- Run uvx pytest -q in the project root to verify.
- IMPORTANT: Use `uvx pytest` (NOT `uv run pytest`) for ALL verification commands. The `uv run` command fails when the project path contains a colon (`:`) — which the worktree directory always does (because of the `git@github.com:owner/repo` URL layout). `uvx pytest` is equivalent and works correctly. Use `-k <pattern>` to select tests (e.g. `uvx pytest -q -k multiply`). NEVER use the pytest `::test_name` node-id syntax with `uvx` (e.g. `uvx pytest file.py::test_foo`) — use `-k` instead.
- Produce an integration branch with all changes.'

BRANCH_NAME="${COMPOSER_LIVE_REPO_BRANCH:-main}"
if [[ "${REPO_URL}" == file://* && -d "${REPO_DIR}/.git" ]]; then
    BRANCH_NAME=$(cd "${REPO_DIR}" && git rev-parse --abbrev-ref HEAD)
fi

# ── Disposable starting state proof ───────────────────────────────────────
# Build a unique orphan baseline branch on the remote repo so Composer
# starts from exactly add-only (multiply/divide are guaranteed absent).
# Do NOT mutate the repo's default branch.
BASELINE_SHA=""
BASELINE_BRANCH=""
BASELINE_SETUP_DIR=""
if [[ "${REPO_URL}" == file://* && -d "${REPO_DIR}/.git" ]]; then
    BASELINE_SHA=$(cd "${REPO_DIR}" && git rev-parse HEAD)
else
    BASELINE_BRANCH="composer-live-baseline-$(date +%s)"
    BASELINE_SETUP_DIR="/tmp/composer-baseline-setup-$(date +%s)"
    STAGE="baseline setup"
    echo "--- Establish Disposable Baseline (orphan branch) ---"

    # 1. Clone into a throwaway setup directory
    if ! git clone "${REPO_URL}" "${BASELINE_SETUP_DIR}" 2>/dev/null; then
        _fail "baseline clone failed"
    fi

    cd "${BASELINE_SETUP_DIR}" || _fail "cannot cd into baseline setup dir"

    # 2. Configure Git identity (CI runners lack global config)
    git config user.email "composer-e2e@local" 2>/dev/null
    git config user.name     "Composer Live E2E"   2>/dev/null

    # 3. Create orphan branch
    if ! git checkout --orphan "${BASELINE_BRANCH}" 2>/dev/null; then
        _fail "baseline checkout --orphan failed"
    fi

    # 4. Remove all tracked content from staging (orphan starts empty)
    git rm -rf --quiet . 2>/dev/null || true

    # 4. Populate with exactly the add-only files
    mkdir -p calculator
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
dependencies = ["pytest>=7"]

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
minversion = "7.0"
testpaths = ["calculator"]
EOF

    # 5. Assert locally: add works
    INITIAL_ADD=$(python3 -c "from calculator import add; print(add(1,2))" 2>/dev/null || echo "err")
    if [[ "${INITIAL_ADD}" != "3" ]]; then
        _fail "baseline add(1,2) != 3 (got ${INITIAL_ADD})"
    fi
    echo "  PASS: baseline add works"

    # 6. Assert: multiply is absent
    if python3 -c "from calculator import multiply" 2>/dev/null; then
        _fail "baseline has multiply"
    fi
    echo "  PASS: baseline has no multiply"

    # 7. Assert: divide is absent
    if python3 -c "from calculator import divide" 2>/dev/null; then
        _fail "baseline has divide"
    fi
    echo "  PASS: baseline has no divide"

    # 8. Assert: baseline tests pass
    if ! uv sync --quiet 2>&1 || ! uv run --quiet pytest -q 2>&1; then
        _fail "baseline pytest failed"
    fi
    echo "  PASS: baseline pytest passes"

    # 9. Commit baseline
    git add calculator/__init__.py calculator/test_calculator.py pyproject.toml
    if ! git commit --quiet -m "baseline: add-only calculator" 2>/dev/null; then
        _fail "baseline commit failed"
    fi

    BASELINE_SHA=$(git rev-parse HEAD 2>/dev/null) || BASELINE_SHA=""
    if [[ -z "${BASELINE_SHA}" ]]; then
        _fail "baseline rev-parse failed"
    fi
    echo "  baseline_sha=${BASELINE_SHA}"

    # 10. Push baseline branch
    if ! git push origin "${BASELINE_BRANCH}" 2>/dev/null; then
        _fail "baseline push failed"
    fi
    echo "  PASS: baseline branch pushed"

    cd - >/dev/null
    rm -rf "${BASELINE_SETUP_DIR}" 2>/dev/null || true
    BASELINE_SETUP_DIR=""

    # Use the baseline branch as the Composer target
    BRANCH_NAME="${BASELINE_BRANCH}"
fi
echo "  baseline_sha=${BASELINE_SHA}  target_branch=${BRANCH_NAME}"

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
poll_until "tasks dispatched" $((TIMEOUT_SEC / 3)) "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
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
" 2>/dev/null) || WT_RESULT="paths=0 branches=0"

# The two implementation tasks dispatch staggered by tens of seconds;
# the divide task's worktree record may not exist yet right after
# `tasks dispatched`.  Retry the WT query a few times until both are
# registered or we give up after ~3 minutes.
WT_WAIT=0
while ! echo "${WT_RESULT}" | grep -qE "paths=(2|3|4|5)"; do
    if [[ "${WT_WAIT}" -ge 180 ]]; then
        break
    fi
    sleep 15
    WT_WAIT=$((WT_WAIT + 15))
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
" 2>/dev/null) || WT_RESULT="paths=0 branches=0"
    echo "  worktree uniqueness: ${WT_RESULT} (after ${WT_WAIT}s)"
done
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
check_contains "0 required command failures" "failures=0" "${VERIF_RESULT}"

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

# ── 11b. Integration branch was pushed to remote (auto_push proof) ──────────
# Correct contract: list artifacts, find result.json by name, get its
# artifact ID, download that artifact (not the GW task ID).
STAGE="check integration pushed"
echo "--- Integration Push Proof ---"
INTEG_PUSHED=$(echo "$R" | python3 -c "
import sys, json, os, urllib.request
tasks = json.load(sys.stdin).get('tasks', [])
gw_base = os.environ.get('CONDUCTOR_AGENTS_GATEWAY_URL', '')
gw_token = os.environ.get('CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN', '')
headers = {}
if gw_token:
    headers['X-Auth-Internal-Token'] = gw_token
it = [t for t in tasks if t.get('node_key')=='integration']
if not it or not it[0].get('agents_gateway_task_id'):
    print('error:no_integration_task')
    sys.exit(0)
gw_task_id = it[0]['agents_gateway_task_id']

try:
    # 1. Get artifact list
    req = urllib.request.Request(f'{gw_base}/tasks/{gw_task_id}/artifacts', headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        arts = json.loads(resp.read()).get('artifacts', [])
except Exception:
    print('error:artifact_list_failed')
    sys.exit(0)

# 2. Find result.json
result_art = None
for a in arts:
    if a.get('name') == 'result.json':
        result_art = a
        break
if not result_art:
    print('error:result_json_missing')
    sys.exit(0)
art_id = result_art.get('id', '')
if not art_id:
    print('error:artifact_id_missing')
    sys.exit(0)

# 3. Download result.json
try:
    req = urllib.request.Request(f'{gw_base}/artifacts/{art_id}?view=true', headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read()
        result = json.loads(data) if isinstance(data, bytes) else json.loads(str(data))
except Exception:
    print('error:artifact_download_failed')
    sys.exit(0)

# 4. Parse git.pushed
try:
    git_info = result.get('git', {}) if isinstance(result, dict) else {}
    pushed = str(git_info.get('pushed', False)).lower()
except Exception:
    pushed = 'error:parse_failed'
    print('error:json_parse_failed')
    sys.exit(0)

if pushed != 'true':
    print(f'error:git_pushed_is_{pushed}')
else:
    print(pushed)
" 2>/dev/null || echo "error")
check "integration branch pushed to remote (auto_push required)" "true" "${INTEG_PUSHED}"

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
# ── 13b. Prove that work was actually done — final commit SHA must
#       differ from the baseline starting SHA.
STAGE="check commit delta"
echo "--- Commit Delta Proof ---"
if [[ -n "${BASELINE_SHA}" && -n "${FINAL_SH}" ]]; then
    if [[ "${BASELINE_SHA}" != "${FINAL_SH}" ]]; then
        echo "  PASS: final commit differs from baseline (work was done)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: final commit SHA (${FINAL_SH}) == baseline SHA (${BASELINE_SHA}) — no work done"
        FAIL=$((FAIL + 1))
    fi
else
    echo "  WARN: cannot compare — baseline or final SHA missing"
    FAIL=$((FAIL + 1))
fi

# ── 15. Import multiply and divide from the final checkout ─────────────────
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
if [[ -d "${VERIFY_DIR}" ]] && cd "${VERIFY_DIR}" && uv sync --quiet 2>/dev/null && uv run --quiet pytest -q 2>/dev/null; then
    echo "  PASS: pytest passed against final checkout"
    PASS=$((PASS + 1))
else
    echo "  FAIL: pytest failed against final checkout"
    FAIL=$((FAIL + 1))
fi
cd - >/dev/null 2>/dev/null || true

# ── 16. HTML and JSON report content served via HTTP ───────────────────────
STAGE="check reports"
echo "--- Reports (via HTTP) ---"
poll_until "report exists" 120 "${BASE}/composer/objectives/${OBJ_ID}/report" \
    "import sys,json; d=json.load(sys.stdin); print(d.get('json_artifact_ref',''))" \
    "[a-z]"

# Fetch the JSON report content via the dedicated HTTP endpoint
REPORT_JSON=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/report/json" 2>/dev/null || echo "{}")
REPORT_HTML=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/report/html" 2>/dev/null || echo "")
check_contains "JSON report content fetched via HTTP" "objective_id" "${REPORT_JSON}"
check_contains "HTML report content fetched via HTTP" "<html" "${REPORT_HTML}"

# ── 17. Reports contain downstream artifact references ────────────────────
STAGE="check report downstream artifacts"
echo "--- Downstream Artifact References ---"
HAS_VERIF=$(echo "${REPORT_JSON}" | python3 -c "import sys,json; d=json.load(sys.stdin); v=d.get('verification',[]); print(len(v))" 2>/dev/null || echo "0")
check_contains "report has verification rows" "1\|2\|3\|4\|5" "${HAS_VERIF}"
HAS_TASKS=$(echo "${REPORT_JSON}" | python3 -c "import sys,json; d=json.load(sys.stdin); t=d.get('task_graph',[]); print(len(t))" 2>/dev/null || echo "0")
check_contains "report has task_graph entries" "1\|2\|3\|4\|5" "${HAS_TASKS}"
HAS_ARTIFACTS=$(echo "${REPORT_JSON}" | python3 -c "import sys,json; d=json.load(sys.stdin); a=d.get('downstream_artifacts',[]); print(len(a))" 2>/dev/null || echo "0")
check_contains "report has downstream_artifacts entries" "1\|2\|3\|4\|5" "${HAS_ARTIFACTS}"

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
    echo ""
    echo "COMPOSER LIVE E2E PASSED"
    _CANONICAL_PRINTED=true
    E2E_CLEAN_OK=1
    exit 0
else
    echo ""
    echo "COMPOSER LIVE E2E FAILED: ${STAGE}"
    _CANONICAL_PRINTED=true
    exit 1
fi
