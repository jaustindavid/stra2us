#!/usr/bin/env bash
# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
# v1.7.2 Sprint 6: device-flow smoke test.
#
# Complements tools/smoke_test.sh — that one verifies hostnames respond
# and auth redirects are correct, but never exercises the actual HMAC-
# signed device protocol against /q/ and /kv/. A regression in HMAC
# signing, msgpack framing, or response-signature verification slips
# through `smoke_test.sh`. This script catches it by driving the same
# protocol the real ESP32 client uses, through the Python CLI's
# `stra2us synth-traffic` action loop.
#
# It's intentionally short — the heavy lifting lives in the CLI; the
# script just provisions the env vars, runs the loop for a few seconds,
# and translates the CLI's exit code into the same PASS/FAIL format
# `smoke_test.sh` uses, so a unified `tools/stage smoke` reads as one
# coherent report.
#
# Required env:
#   STRA2US_HOST           e.g. http://iot-staging.stra2us.austindavid.com:8253
#   STRA2US_CLIENT_ID      e.g. smoke-test-device
#   STRA2US_SECRET_HEX     64-char hex (the HMAC shared secret)
#
# Optional env (defaults shown):
#   STRA2US_SMOKE_APP=_smoke                  the catalog-app namespace
#   STRA2US_SMOKE_DURATION=2s                 must be a valid synth-traffic duration
#   STRA2US_SMOKE_RATE=1                      ticks per second
#
# The KV key and queue topic are derived from the app + client_id so
# the ACL shape provisioned by `tools/stage seed-smoke-device` covers
# both surfaces without extra plumbing. Topic ends in `/heartbeep` to
# match the convention in existing critterchron traffic, so an operator
# eyeballing the activity log sees these alongside real device pings.
#
# Exit code 0 = all device-flow checks passed; nonzero = at least one
# failed (count printed at the end).

set -u

PASS=0
FAIL=0

# Same report format as smoke_test.sh — operators see a single visual
# style across both scripts.
report() {
    local name="$1" status="$2" detail="$3"
    case "$status" in
        ok)   printf "  [PASS] %-50s %s\n" "$name" "$detail"; PASS=$((PASS+1)) ;;
        skip) printf "  [SKIP] %-50s %s\n" "$name" "$detail" ;;
        *)    printf "  [FAIL] %-50s %s\n" "$name" "$detail"; FAIL=$((FAIL+1)) ;;
    esac
}

# ----- preflight -----

missing=()
[[ -n "${STRA2US_HOST:-}" ]]         || missing+=("STRA2US_HOST")
[[ -n "${STRA2US_CLIENT_ID:-}" ]]    || missing+=("STRA2US_CLIENT_ID")
[[ -n "${STRA2US_SECRET_HEX:-}" ]]   || missing+=("STRA2US_SECRET_HEX")

if (( ${#missing[@]} > 0 )); then
    echo "smoke_test_device.sh: missing required env: ${missing[*]}" >&2
    echo "smoke_test_device.sh: invoke via 'tools/stage smoke-device' to populate from staging." >&2
    exit 2
fi

SMOKE_APP="${STRA2US_SMOKE_APP:-_smoke}"
SMOKE_DURATION="${STRA2US_SMOKE_DURATION:-2s}"
SMOKE_RATE="${STRA2US_SMOKE_RATE:-1}"

QUEUE_TOPIC="${SMOKE_APP}/public/heartbeep"
KV_KEY="${SMOKE_APP}/${STRA2US_CLIENT_ID}/synth_test"

# Locate the stra2us CLI. The repo's venv at tools/venv/bin/stra2us is
# the canonical location for dev hosts; if it's missing, fall back to
# whatever's on PATH (CI / container) so a fresh checkout works.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -x "$SCRIPT_DIR/venv/bin/stra2us" ]]; then
    STRA2US_BIN="$SCRIPT_DIR/venv/bin/stra2us"
elif command -v stra2us >/dev/null 2>&1; then
    STRA2US_BIN="stra2us"
else
    echo "smoke_test_device.sh: no stra2us CLI on PATH and no venv at $SCRIPT_DIR/venv/bin/stra2us" >&2
    echo "   bootstrap: cd $SCRIPT_DIR && python3 -m venv venv && venv/bin/pip install -e ." >&2
    exit 2
fi

# ----- checks --------------------------------------------------------

echo "== stra2us device-flow smoke test =="
echo "host          : $STRA2US_HOST"
echo "client_id     : $STRA2US_CLIENT_ID"
echo "queue topic   : $QUEUE_TOPIC"
echo "kv key        : $KV_KEY"
echo "duration/rate : $SMOKE_DURATION @ ${SMOKE_RATE}Hz, mode=both"
echo

# One synth-traffic run at mode=both exercises:
#   1. POST /q/<topic>     (queue write, signed)
#   2. POST /kv/<key>      (KV write,    signed)
#   3. GET  /kv/<key>      (KV read,     response signature + msgpack decode + round-trip equality)
#
# Each per-call response signature is verified inside Stra2usClient; a
# signature mismatch raises Stra2usError and bumps the corresponding
# error counter. The CLI exits 4 if total_errors > 0 (clean) or 0
# (clean), so we just check exit code + capture stdout for diagnostic
# context on failure.
#
# Note we explicitly pass --mode=both so an operator overriding env
# can't accidentally turn off one half of the protocol coverage.

summary_file=$(mktemp)
trap 'rm -f "$summary_file"' EXIT

set +e
"$STRA2US_BIN" synth-traffic \
    --queue   "$QUEUE_TOPIC" \
    --kv-key  "$KV_KEY" \
    --duration "$SMOKE_DURATION" \
    --rate    "$SMOKE_RATE" \
    --mode    both \
    > "$summary_file" 2>&1
rc=$?
set -e

summary=$(cat "$summary_file")

# Extract per-call counts from the summary line. Format (from
# synth.SynthResult.summary_line):
#   synth-traffic: 2.0s elapsed, 6 calls (2 q-POST, 2 kv-PUT, 2 kv-GET), 0 errors (3.00 Hz)
extract() {
    # $1 = label after a number, e.g. "q-POST"
    printf "%s" "$summary" | grep -oE "[0-9]+ $1" | head -1 | grep -oE "^[0-9]+"
}
q_posts=$(extract "q-POST")
kv_puts=$(extract "kv-PUT")
kv_gets=$(extract "kv-GET")
errors=$(printf "%s" "$summary" | grep -oE "[0-9]+ errors" | head -1 | grep -oE "^[0-9]+")
: "${q_posts:=0}" "${kv_puts:=0}" "${kv_gets:=0}" "${errors:=?}"

# Whole-loop pass/fail. The CLI bakes the verdict into rc; we just
# surface the count breakdown so a partial failure (e.g. queue works
# but KV doesn't) is obvious without re-running.
if [[ "$rc" == "0" ]]; then
    report "queue POSTs (signed)"               ok "${q_posts} ok"
    report "kv PUTs (signed)"                   ok "${kv_puts} ok"
    report "kv GETs (signed + round-trip)"      ok "${kv_gets} ok, round-trips verified"
else
    # rc=4 → call(s) completed but with errors. rc=2 → arg-parse /
    # config error before the loop even started. Different mitigations,
    # so flag them distinctly.
    if [[ "$rc" == "2" ]]; then
        report "device-flow smoke setup"        fail "CLI rejected args/config; see summary below"
    else
        report "queue POSTs (signed)"           fail "${q_posts} ok / ${errors} errors total"
        report "kv PUTs (signed)"               fail "${kv_puts} ok / ${errors} errors total"
        report "kv GETs (signed + round-trip)"  fail "${kv_gets} ok / ${errors} errors total"
    fi
    echo
    echo "  --- synth-traffic output ---"
    printf "%s\n" "$summary" | sed 's/^/  /'
    echo "  ---"
fi

echo
echo "== summary =="
echo "passed:  $PASS"
echo "failed:  $FAIL"
exit $(( FAIL > 0 ? 1 : 0 ))
