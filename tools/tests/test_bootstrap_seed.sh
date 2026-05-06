#!/usr/bin/env bash
# tools/tests/test_bootstrap_seed.sh — verify seed_htpasswd merge logic.
#
# Runs locally, no docker / SSH / staging cycle. Three test cases:
#   1. Fresh host (admin.htpasswd absent)         → full copy of default
#   2. Existing without rescue                    → rescue appended,
#                                                    others preserved
#   3. Existing already has rescue (different)    → no change, no dup,
#                                                    operator's value
#                                                    preserved
#
# Exit 0 if all assertions pass, 1 otherwise.
#
# Usage: tools/tests/test_bootstrap_seed.sh

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Source the function under test. bootstrap-host.sh's bootstrap body
# is gated on "invoked as script, not sourced" so this is safe.
# shellcheck source=../bootstrap-host.sh
source "$REPO_ROOT/tools/bootstrap-host.sh"

PASS=0
FAIL=0
TEST_DIR=""

assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        printf "  [PASS] %s\n" "$name"
        PASS=$((PASS + 1))
    else
        printf "  [FAIL] %s\n" "$name"
        printf "        expected: %q\n" "$expected"
        printf "        actual:   %q\n" "$actual"
        FAIL=$((FAIL + 1))
    fi
}

setup() {
    if [[ -n "$TEST_DIR" && -d "$TEST_DIR" ]]; then
        rm -rf "$TEST_DIR"
    fi
    TEST_DIR=$(mktemp -d)
    mkdir -p "$TEST_DIR/backend"
    # Default ships rescue with a known fake hash. Single quotes prevent
    # variable expansion of $fakehash.
    printf 'rescue:fakesalt$fakehash\n' > "$TEST_DIR/backend/admin.htpasswd.default"
}

cleanup() {
    if [[ -n "$TEST_DIR" && -d "$TEST_DIR" ]]; then
        rm -rf "$TEST_DIR"
    fi
}
trap cleanup EXIT

# --- Test 1 -----------------------------------------------------------
echo "[Test 1] fresh host — admin.htpasswd absent"
setup
seed_htpasswd "$TEST_DIR" >/dev/null
assert_eq "live file created" \
    "yes" \
    "$([[ -f "$TEST_DIR/backend/admin.htpasswd" ]] && echo yes || echo no)"
assert_eq "live file content matches default" \
    "$(cat "$TEST_DIR/backend/admin.htpasswd.default")" \
    "$(cat "$TEST_DIR/backend/admin.htpasswd")"

# --- Test 2 -----------------------------------------------------------
echo
echo "[Test 2] existing admin.htpasswd without rescue user"
setup
{
    printf 'smoke:smokesalt$smokehash\n'
    printf 'admin:adminsalt$adminhash\n'
} > "$TEST_DIR/backend/admin.htpasswd"
seed_htpasswd "$TEST_DIR" >/dev/null
assert_eq "smoke still present"     "1" \
    "$(grep -c '^smoke:' "$TEST_DIR/backend/admin.htpasswd")"
assert_eq "admin still present"     "1" \
    "$(grep -c '^admin:' "$TEST_DIR/backend/admin.htpasswd")"
assert_eq "rescue added"            "1" \
    "$(grep -c '^rescue:' "$TEST_DIR/backend/admin.htpasswd")"
assert_eq "rescue line is from default (verbatim)" \
    'rescue:fakesalt$fakehash' \
    "$(grep '^rescue:' "$TEST_DIR/backend/admin.htpasswd")"

# --- Test 3 -----------------------------------------------------------
echo
echo "[Test 3] existing admin.htpasswd already has rescue (different hash)"
setup
printf 'rescue:OPERATOR$theirhash\n' > "$TEST_DIR/backend/admin.htpasswd"
seed_htpasswd "$TEST_DIR" >/dev/null
assert_eq "no duplicate rescue lines" "1" \
    "$(grep -c '^rescue:' "$TEST_DIR/backend/admin.htpasswd")"
assert_eq "operator's rescue preserved (default does NOT overwrite)" \
    'rescue:OPERATOR$theirhash' \
    "$(grep '^rescue:' "$TEST_DIR/backend/admin.htpasswd")"

# --- summary ----------------------------------------------------------
echo
echo "passed: $PASS"
echo "failed: $FAIL"
exit $((FAIL > 0 ? 1 : 0))
