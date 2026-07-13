#!/usr/bin/env bash
# Live Composer E2E — spec-to-verified-execution with real Composer LLM,
# real Agents Gateway, real harness sessions.
#
# Must create or accept a disposable Git repository, submit a calculator spec,
# poll state progression, and prove each stage of the pipeline.
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
        echo "  FAIL: ${label} (expected pattern '${pattern}')"
        FAIL=$((FAIL + 1))
    fi
}

poll_until() {
    local desc="$1" max_sec="$2" url="$3" extract_py="$4" expected="$5"
    STAGE="${desc}"
    local waited=0
    while [[ $waited -lt $max_sec ]]; do
        local R val
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
echo "Agents Gateway: ${CONDUCTOR_AGENTS_GATEWAY_URL}"
echo "Timeout: ${TIMEOUT_SEC}s"
echo ""

# ── 0. Create disposable repo ──────────────────────────────────────────────
STAGE="setup repo"
echo "--- Setup Repository ---"
REPO_DIR="${COMPOSER_LIVE_REPO_DIR:-/tmp/composer-live-repo-$(date +%s)}"
if [[ ! -d "${REPO_DIR}/.git" ]]; then
    mkdir -p "${REPO_DIR}"
    cd "${REPO_DIR}"
    git init
    git config user.email "composer@e2e.test"
    git config user.name "Composer Live E2E"
    echo '# E2E Calculator — disposable live test repo' > README.md
    mkdir -p calculator
    cat > calculator/__init__.py <<'EOF'
"""Simple calculator package for E2E testing."""

def add(a: int, b: int) -> int:
    return a + b

def multiply(a: int, b: int) -> int:
    return a * b

def divide(a: int, b: int) -> float:
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
EOF
    cat > calculator/test_calculator.py <<'EOF'
import pytest
from calculator import add, multiply, divide

def test_add():
    assert add(2, 3) == 5
    assert add(0, 0) == 0

def test_multiply():
    assert multiply(3, 4) == 12
    assert multiply(0, 5) == 0

def test_divide():
    assert divide(10, 2) == 5.0
    assert divide(9, 3) == 3.0

def test_divide_by_zero():
    with pytest.raises(ValueError, match="Cannot divide by zero"):
        divide(1, 0)
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
    git commit -m "Initial calculator scaffold"
    cd -
fi
echo "  Repo: ${REPO_DIR}"
echo "  Branch: $(cd "${REPO_DIR}" && git rev-parse --abbrev-ref HEAD)"

# ── 1. Health ─────────────────────────────────────────────────────────────
STAGE="health"
echo "--- Health ---"
R=$(curl -sf "${BASE}/health")
check "health ok" "ok" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")"

# ── 2. Submit calculator specification — returns immediately ───────────────
STAGE="submit spec"
echo "--- Submit Spec (async) ---"

SPEC_TEXT='Extend the calculator package with multiply and divide functions.
Requirements:
- Add multiply(a,b) returning a*b.
- Add divide(a,b) returning a/b; raise ValueError for b=0.
- Use the repository and base branch provided.
- Include pytest tests in calculator/test_calculator.py.
- Run uv run pytest -q in the project root.
- Produce an integration branch with all changes.'

REPO_URL="${COMPOSER_LIVE_REPO_URL:-file://${REPO_DIR}}"
BRANCH_NAME=$(cd "${REPO_DIR}" && git rev-parse --abbrev-ref HEAD)

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

# ── 3. Repository and base branch persisted ────────────────────────────────
echo "--- Repository Preservation ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/spec")
PERSISTED_URL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('repository_url',''))")
PERSISTED_BR=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_branch',''))")
check_contains "repo url persisted" "${REPO_URL}" "${PERSISTED_URL}"
check "base branch persisted" "${BRANCH_NAME}" "${PERSISTED_BR}"

# ── 4. Spec advances from received through intermediate states ─────────────
poll_until "spec advanced" 120 "${BASE}/composer/objectives/${OBJ_ID}/spec" \
    "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" \
    "normalized\|planning\|planned\|executing\|integrating\|verifying\|completed"

# ── 5. Plan generated with at least 2 implementation tasks ─────────────────
poll_until "plan generated" 120 "${BASE}/composer/objectives/${OBJ_ID}/plan" \
    "import sys,json; d=json.load(sys.stdin); imp=[t for t in d.get('plan_tasks',[]) if t.get('node_key')!='integration']; print(len(imp))" \
    "2\|3\|4\|5\|6"

# ── 6. Tasks dispatched — at least 2 separate agent gateway worktrees ──────
poll_until "tasks dispatched" 180 "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
    "import sys,json; d=json.load(sys.stdin); dispatched=[t for t in d.get('tasks',[]) if t.get('status') in ('dispatching','running','completed','verifying')]; print(len(dispatched))" \
    "2\|3\|4\|5"

# Verify distinct worktrees
echo "--- Distinct Worktrees ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/tasks")
GW_IDS=$(echo "$R" | python3 -c "
import sys,json
tasks = json.load(sys.stdin).get('tasks',[])
ids = [t.get('agents_gateway_task_id') for t in tasks if t.get('agents_gateway_task_id')]
print(len(set(ids)))
")
check_contains "at least 2 distinct gw task ids" "2\|3\|4\|5" "${GW_IDS}"

# ── 7. Tasks complete with real harness sessions ───────────────────────────
poll_until "implementation tasks completed" $((TIMEOUT_SEC - 200)) "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
    "import sys,json; tasks=json.load(sys.stdin).get('tasks',[]); ct=sum(1 for t in tasks if t.get('task_type')!='integration' and t.get('status')=='completed'); print(ct)" \
    "2\|3\|4\|5"

# ── 8. Integration task dispatched and completed ───────────────────────────
poll_until "integration completed" $((TIMEOUT_SEC - 100)) "${BASE}/composer/objectives/${OBJ_ID}/tasks" \
    "import sys,json; tasks=json.load(sys.stdin).get('tasks',[]); it=[t for t in tasks if t.get('task_type')=='integration' or t.get('node_key')=='integration']; print(it[0].get('status','') if it else 'none')" \
    "completed"

# ── 9. Final branch and commit SHA recorded ────────────────────────────────
STAGE="check branch/commit"
echo "--- Final Branch & Commit ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/report")
FINAL_BR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('final_branch',''))")
FINAL_SH=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('final_commit_sha',''))")
check_contains "final branch present" ".\{3\}" "${FINAL_BR}"
check_contains "final commit sha present" ".\{3\}" "${FINAL_SH}"
echo "  branch=${FINAL_BR}  commit=${FINAL_SH}"

# ── 10. HTML and JSON reports exist ────────────────────────────────────────
STAGE="check reports"
echo "--- Reports ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}/report")
HTML=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('html_artifact_ref',''))")
JSON=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('json_artifact_ref',''))")
check_contains "HTML report ref present" ".\{3\}" "${HTML}"
check_contains "JSON report ref present" ".\{3\}" "${JSON}"
echo "  html=${HTML}"
echo "  json=${JSON}"

# ── 11. Objective completed ────────────────────────────────────────────────
STAGE="check completion"
echo "--- Completion ---"
R=$(curl -sf -H "${AUTH_HEADER}" "${BASE}/composer/objectives/${OBJ_ID}")
STATUS=$(echo "$R" | python3 -c "
import sys,json
d = json.load(sys.stdin)
spec = d.get('composer_spec', {})
print(spec.get('status',''))
")
check "objective completed" "completed" "${STATUS}"

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