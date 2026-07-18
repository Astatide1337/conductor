#!/usr/bin/env bash
# Shell tests for e2e-composer-live.sh prerequisite blocking.
set -uo pipefail

SCRIPT="scripts/e2e-composer-live.sh"
PASS=0
FAIL=0

assert_exit() {
    local label="$1" expected="$2" actual="$3"
    if [ "$actual" -eq "$expected" ]; then
        echo "  PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $label (expected exit ${expected}, got ${actual})"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local label="$1" pattern="$2" text="$3"
    if echo "$text" | grep -q "$pattern"; then
        echo "  PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $label (expected pattern '$pattern' not found in: $text)"
        FAIL=$((FAIL + 1))
    fi
}

assert_not_contains() {
    local label="$1" pattern="$2" text="$3"
    if echo "$text" | grep -q "$pattern"; then
        echo "  FAIL: $label (forbidden pattern '$pattern' found in: $text)"
        FAIL=$((FAIL + 1))
    else
        echo "  PASS: $label"
        PASS=$((PASS + 1))
    fi
}

echo "=== E2E Prerequisite Block Tests ==="
echo ""

# ── Test 1: Missing all required environment variables ──────────────────────
echo "--- Test 1: missing required environment variables ---"
OUTPUT=$(bash "$SCRIPT" 2>&1) && EXIT=$? || EXIT=$?
assert_exit "exit code 2 for missing env" "2" "$EXIT"
assert_contains "canonical BLOCKED message" \
    "COMPOSER LIVE E2E BLOCKED: prerequisites — missing" "$OUTPUT"
assert_not_contains "no command not found" "command not found" "$OUTPUT"
assert_not_contains "no unbound variable" "unbound variable" "$OUTPUT"
assert_not_contains "no FAILED" "COMPOSER LIVE E2E FAILED" "$OUTPUT"

echo ""

# ── Test 2: AGW_HARNESS__AUTO_PUSH=false ─────────────────────────────────────
echo "--- Test 2: AGW_HARNESS__AUTO_PUSH=false ---"
OUTPUT=$(env \
    CONDUCTOR_BASE_URL=http://localhost:8093 \
    CONDUCTOR_AUTH_MODE=internal \
    CONDUCTOR_INTERNAL_TOKEN=fake \
    CONDUCTOR_COMPOSER_LLM_BASE_URL=http://localhost:8093 \
    CONDUCTOR_COMPOSER_LLM_API_KEY=fake \
    CONDUCTOR_COMPOSER_LLM_MODEL=fake \
    CONDUCTOR_AGENTS_GATEWAY_URL=http://localhost:8092 \
    CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE=internal \
    CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN=fake \
    AGW_HARNESS__AUTO_PUSH=false \
    COMPOSER_LIVE_REPO_URL=file:///tmp/fake \
    bash "$SCRIPT" 2>&1) && EXIT=$? || EXIT=$?
assert_exit "exit code 2 for invalid auto_push" "2" "$EXIT"
assert_contains "canonical BLOCKED for auto_push" \
    "COMPOSER LIVE E2E BLOCKED: prerequisites — AGW_HARNESS__AUTO_PUSH must be true" "$OUTPUT"
assert_not_contains "no command not found" "command not found" "$OUTPUT"
assert_not_contains "no unbound variable" "unbound variable" "$OUTPUT"
assert_contains "shows the actual value" "false" "$OUTPUT"

echo ""

# ── Test 3: AGW_HARNESS__AUTO_PUSH=0 ─────────────────────────────────────────
echo "--- Test 3: AGW_HARNESS__AUTO_PUSH=0 ---"
OUTPUT=$(env \
    CONDUCTOR_BASE_URL=http://localhost:8093 \
    CONDUCTOR_AUTH_MODE=internal \
    CONDUCTOR_INTERNAL_TOKEN=fake \
    CONDUCTOR_COMPOSER_LLM_BASE_URL=http://localhost:8093 \
    CONDUCTOR_COMPOSER_LLM_API_KEY=fake \
    CONDUCTOR_COMPOSER_LLM_MODEL=fake \
    CONDUCTOR_AGENTS_GATEWAY_URL=http://localhost:8092 \
    CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE=internal \
    CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN=fake \
    AGW_HARNESS__AUTO_PUSH=0 \
    COMPOSER_LIVE_REPO_URL=file:///tmp/fake \
    bash "$SCRIPT" 2>&1) && EXIT=$? || EXIT=$?
assert_exit "exit code 2 for auto_push=0" "2" "$EXIT"
assert_contains "auto_push=0 is invalid" \
    "AGW_HARNESS__AUTO_PUSH must be true/1/yes" "$OUTPUT"
assert_not_contains "no FAILED" "COMPOSER LIVE E2E FAILED" "$OUTPUT"

echo ""

# ── Test 4: missing CONDUCTOR_ENVIRONMENT (not production) ───────────────────
echo "--- Test 4: CONDUCTOR_ENVIRONMENT missing ---"
OUTPUT=$(env \
    CONDUCTOR_BASE_URL=http://localhost:8093 \
    CONDUCTOR_AUTH_MODE=internal \
    CONDUCTOR_INTERNAL_TOKEN=fake \
    CONDUCTOR_COMPOSER_LLM_BASE_URL=http://localhost:8093 \
    CONDUCTOR_COMPOSER_LLM_API_KEY=fake \
    CONDUCTOR_COMPOSER_LLM_MODEL=fake \
    CONDUCTOR_AGENTS_GATEWAY_URL=http://localhost:8092 \
    CONDUCTOR_AGENTS_GATEWAY_AUTH_MODE=internal \
    CONDUCTOR_AGENTS_GATEWAY_INTERNAL_TOKEN=fake \
    AGW_HARNESS__AUTO_PUSH=true \
    COMPOSER_LIVE_REPO_URL=file:///tmp/fake \
    bash "$SCRIPT" 2>&1) && EXIT=$? || EXIT=$?
assert_exit "exit 2 for missing CONDUCTOR_ENVIRONMENT" "2" "$EXIT"
assert_contains "canonical BLOCKED for env" \
    "COMPOSER LIVE E2E BLOCKED: prerequisites — CONDUCTOR_ENVIRONMENT must be 'production'" "$OUTPUT"

echo ""

# ── Results ──────────────────────────────────────────────────────────────────
echo "=== Results ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"

if [ "$FAIL" -eq 0 ]; then
    echo "Prerequisite block tests passed"
    exit 0
else
    echo "Prerequisite block tests FAILED"
    exit 1
fi