#!/usr/bin/env bash
# Functional smoke test for the stra2us deployment. Validates that
# device traffic, admin auth, and OAuth routes behave as expected.
#
# Run after any rebuild, especially when requirements.txt or the
# Docker image has changed. Exit code 0 = all checks passed,
# nonzero = at least one check failed (count printed at the end).
#
# Required: bash, curl. No Python or other deps.
#
# Configure via env vars (defaults shown):
#   STRA2US_BROWSER_HOST=stra2us.austindavid.com
#   STRA2US_DEVICE_HOST=iot.stra2us.austindavid.com
#   STRA2US_DEVICE_PORT=8153
#   SMOKE_ADMIN_USER, SMOKE_ADMIN_PASS — optional; if set, the
#       activity-log check runs against /api/admin/logs to confirm
#       a recent device heartbeat. Skipped when unset.
#
# Usage: tools/smoke_test.sh [--quick]
#   --quick: skip checks that require credentials.
#
# ---------------------------------------------------------------------
# One-time setup: creating the smoke-test user
# ---------------------------------------------------------------------
# The activity-log check needs an admin account with wildcard ACL
# coverage. Two layers — htpasswd auth, then Redis-backed ACL — and
# the user must exist in both.
#
# 1) Create the htpasswd entry. The helper writes the bespoke
#    salt$sha256(salt+password) format admin_auth.py expects; the
#    standard `htpasswd` CLI will NOT produce a working entry.
#
#       cd backend && python3 create_admin.py smoke 'pick-a-password' && cd ..
#
#    The file (`backend/admin.htpasswd`) is bind-mounted into the
#    container, so the edit takes effect immediately — no rebuild.
#
# 2) Provision the ACL row. Wildcard prefix so the smoke user sees
#    every activity-log entry; without this, the log filter silently
#    drops everything and the freshness check reports a false miss.
#
#       docker compose exec stra2us-iot redis-cli SET \
#         'admin_acls:smoke' '{"permissions":[{"prefix":"*","access":"rw"}]}'
#
# 3) Run with creds:
#
#       SMOKE_ADMIN_USER=smoke SMOKE_ADMIN_PASS='pick-a-password' \
#         tools/smoke_test.sh
#
# Reusing an existing wildcard admin works too — skip step 1+2 if
# you already have one.

set -u

BROWSER_HOST="${STRA2US_BROWSER_HOST:-stra2us.austindavid.com}"
DEVICE_HOST="${STRA2US_DEVICE_HOST:-iot.stra2us.austindavid.com}"
DEVICE_PORT="${STRA2US_DEVICE_PORT:-8153}"

BROWSER_BASE="https://${BROWSER_HOST}"
DEVICE_BASE="http://${DEVICE_HOST}:${DEVICE_PORT}"

QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

PASS=0
FAIL=0
SKIP=0

# --- helpers ---------------------------------------------------------

# Print a check result. $1=name, $2=ok|fail|skip, $3=detail
report() {
    local name="$1" status="$2" detail="$3"
    case "$status" in
        ok)
            printf "  [PASS] %-50s %s\n" "$name" "$detail"
            PASS=$((PASS+1))
            ;;
        skip)
            printf "  [SKIP] %-50s %s\n" "$name" "$detail"
            SKIP=$((SKIP+1))
            ;;
        *)
            printf "  [FAIL] %-50s %s\n" "$name" "$detail"
            FAIL=$((FAIL+1))
            ;;
    esac
}

# Run a curl that returns just the HTTP status code.
http_code() {
    curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$@"
}

# Run a curl, capture status + a header value (case-insensitive header name).
# Args: header_name url [extra curl args...]
http_status_header() {
    local header="$1" url="$2"
    shift 2
    local resp
    resp=$(curl -s -D - -o /dev/null --max-time 10 "$@" "$url")
    local code header_val
    # Use the LAST status line (HTTP/...) — with HTTP/2 + CF, there
    # can be 1xx informational responses (103 Early Hints, etc.)
    # before the final 2xx/3xx/4xx. head -1 would pick the early
    # hint; tail -1 picks the final response.
    code=$(printf "%s" "$resp" | grep -E '^HTTP/' | tail -1 | awk '{print $2}')
    # Same logic for the header value: take the LAST occurrence,
    # since headers like Location only appear on the final response.
    header_val=$(printf "%s" "$resp" | grep -i "^${header}:" | tail -1 | sed -E 's/^[^:]+:[[:space:]]*//' | tr -d '\r')
    printf "%s|%s" "$code" "$header_val"
}

# --- checks ----------------------------------------------------------

echo "== stra2us smoke test =="
echo "browser host : $BROWSER_BASE"
echo "device host  : $DEVICE_BASE"
echo

echo "[health]"
code=$(http_code "${BROWSER_BASE}/health")
[[ "$code" == "200" ]] && report "browser /health"  ok   "200" || report "browser /health"  fail "got $code"
code=$(http_code "${DEVICE_BASE}/health")
[[ "$code" == "200" ]] && report "device /health"   ok   "200" || report "device /health"   fail "got $code"

echo
echo "[admin auth — browser host redirects to OAuth, device host htpasswd (rescue)]"
# Browser host: no cookie → 302 to /oauth/google/login (Phase 4).
# We don't follow the redirect; we just verify it points where it should.
res=$(http_status_header "location" "${BROWSER_BASE}/admin/")
code="${res%%|*}"; loc="${res##*|}"
if [[ "$code" == "302" && "$loc" == */oauth/google/login* ]]; then
    report "browser /admin/ → 302 OAuth" ok "${loc:0:70}"
else
    report "browser /admin/ → 302 OAuth" fail "code=$code loc='$loc'"
fi
# Device host: still htpasswd. This is the rescue path — it must NOT
# break when the browser-host OAuth redirect is in effect.
res=$(http_status_header "www-authenticate" "${DEVICE_BASE}/admin/")
code="${res%%|*}"; auth="${res##*|}"
if [[ "$code" == "401" && "$auth" == Basic* ]]; then
    report "device /admin/ → 401 Basic (rescue)" ok "$auth"
else
    report "device /admin/ → 401 Basic (rescue)" fail "code=$code auth='$auth'"
fi

echo
echo "[oauth routes]"
res=$(http_status_header "location" "${BROWSER_BASE}/oauth/google/login")
code="${res%%|*}"; loc="${res##*|}"
if [[ "$code" == "302" && "$loc" == https://accounts.google.com/* ]]; then
    report "browser /oauth/google/login → Google" ok "302 → ${loc:0:60}..."
else
    report "browser /oauth/google/login → Google" fail "code=$code loc='$loc'"
fi
res=$(http_status_header "location" "${DEVICE_BASE}/oauth/google/login")
code="${res%%|*}"; loc="${res##*|}"
if [[ "$code" == "302" && "$loc" == https://accounts.google.com/* ]]; then
    report "device /oauth/google/login → Google"  ok "302 → ${loc:0:60}..."
else
    report "device /oauth/google/login → Google"  fail "code=$code loc='$loc'"
fi

# Callback without code/state → handler returns 400 (state mismatch).
# Proves the route is reachable WITHOUT a session — the /oauth/ carve-out works.
code=$(http_code "${BROWSER_BASE}/oauth/google/callback")
[[ "$code" == "400" ]] && report "callback w/o code → 400 (carve-out works)" ok "400" \
    || report "callback w/o code → 400 (carve-out works)" fail "got $code"

# A made-up /oauth/ path should 404, not 200/302. Proves the carve-out
# is route-bound, not a wildcard hole.
code=$(http_code "${BROWSER_BASE}/oauth/bogus")
[[ "$code" == "404" ]] && report "/oauth/bogus → 404 (carve-out is route-bound)" ok "404" \
    || report "/oauth/bogus → 404 (carve-out is route-bound)" fail "got $code"

# --- activity log (optional, requires creds) -------------------------

if [[ $QUICK -eq 0 && -n "${SMOKE_ADMIN_USER:-}" && -n "${SMOKE_ADMIN_PASS:-}" ]]; then
    echo
    echo "[activity log — recent device heartbeat]"
    # Capture status + body in one call. Body to a temp, status from -w.
    body_file=$(mktemp)
    code=$(curl -s --max-time 10 \
        -u "${SMOKE_ADMIN_USER}:${SMOKE_ADMIN_PASS}" \
        -o "$body_file" \
        -w "%{http_code}" \
        "${BROWSER_BASE}/api/admin/logs?limit=1")
    body=$(cat "$body_file")
    rm -f "$body_file"

    if [[ "$code" == "401" ]]; then
        report "device heartbeat in last 60s" fail \
            "401 — bad creds (htpasswd: check SMOKE_ADMIN_USER / SMOKE_ADMIN_PASS, or run backend/create_admin.py)"
    elif [[ "$code" == "403" ]]; then
        report "device heartbeat in last 60s" fail \
            "403 — auth ok but forbidden (no admin_acls:${SMOKE_ADMIN_USER} row?)"
    elif [[ "$code" != "200" ]]; then
        report "device heartbeat in last 60s" fail \
            "HTTP $code — unexpected; body: ${body:0:120}"
    else
        # 200 — auth + ACL passed. Either we got entries (with a
        # timestamp) or the filter dropped everything (likely an ACL
        # prefix that doesn't match any client_id).
        ts=$(printf "%s" "$body" | grep -oE '"timestamp"[[:space:]]*:[[:space:]]*[0-9]+' | head -1 | grep -oE '[0-9]+')
        if [[ -z "$ts" ]]; then
            report "device heartbeat in last 60s" fail \
                "200 but no entries — ACL prefix likely too narrow (need wildcard '*'), or activity log is empty"
        else
            now=$(date +%s)
            age=$((now - ts))
            if (( age <= 60 )); then
                report "device heartbeat in last 60s" ok "age=${age}s"
            else
                report "device heartbeat in last 60s" fail \
                    "newest entry is ${age}s old — no device traffic recently (device offline? port 8153 unreachable?)"
            fi
        fi
    fi
elif [[ $QUICK -eq 0 ]]; then
    echo
    echo "[activity log]"
    report "device heartbeat in last 60s" skip "SMOKE_ADMIN_USER/SMOKE_ADMIN_PASS not set"
else
    echo
    echo "[activity log]"
    report "device heartbeat in last 60s" skip "--quick passed"
fi

# --- summary ---------------------------------------------------------

echo
echo "== summary =="
echo "passed:  $PASS"
echo "failed:  $FAIL"
echo "skipped: $SKIP"
exit $(( FAIL > 0 ? 1 : 0 ))
