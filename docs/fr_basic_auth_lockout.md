# FR: Basic Auth brute-force detection & lockout

*Drafted 2026-05-06 — design for review, not yet implemented. Threat
model sketched in [`README.md`](../README.md) "Operator sign-in"
section + the v1.5 cutover discussion (see
[`fr_v15_incremental.md`](fr_v15_incremental.md) Phase 7).*

## Status

**Pending — not yet implemented.** Filed as a follow-up to Phase 7's
"Option A" decision (keep `/admin/` reachable on the device hostname
for rescue, narrow htpasswd to RESCUE_USERS only). Adds defense-in-
depth against online brute force of the rescue Basic Auth path.

## Problem

`http://iot.stra2us.austindavid.com:8153/admin/` (and `/api/admin/*`
on the same hostname) accept HTTP Basic Auth. The path must stay
internet-reachable: it's the rescue route operators count on when
the OAuth path or Cloudflare tunnel is broken. We've decided port
8153 cannot move (Phase 7 Option A).

The rescue user has wildcard ACL via the `RESCUE_USERS` pattern.
A successful Basic Auth attempt grants operator-level access. Today
there is **no rate limit, no lockout, and no logging** of failed
attempts. An attacker who finds the endpoint (Shodan, port-scan,
indexed by a fingerprinting service) can attempt password guesses
at an unbounded rate.

The strong-password mitigation makes brute force practically
infeasible at typical-attacker rates, but:
- "Practically infeasible" is not "impossible." Targeted attackers
  with time and bandwidth can still try.
- Without logging, we'd never *know* an attack was in progress.
- A weak password (operator mistake, default not yet rotated, etc.)
  becomes a single point of catastrophic failure with no friction
  on the attacker.

## Threat model

**In scope:**
- Online brute-force / credential-stuffing against Basic Auth on
  `/admin/*` and `/api/admin/*`.
- Detection of attack patterns (bursts of failures, distributed
  guessing, sustained low-rate probing).
- Operator visibility into attack attempts (logs, alerts).

**Out of scope:**
- Offline attacks on captured credentials (mitigated by rotation
  policy, password strength, TLS-on-the-LAN if you ever add it).
- DDoS / volumetric attacks on the service generally (different
  problem; would need rate-limit on `/q/`, `/kv/` too).
- Compromise of the rescue user's password through phishing or
  exfiltration from operator-side storage.

## Design

### Detection: sliding-window failure counter

Keyed by `(source_ip, username)`:
- A failed Basic Auth attempt increments a counter for that
  (IP, username) pair.
- Counter is sliding-window: failures older than `WINDOW_SECONDS`
  drop off. Implementation candidate: Redis sorted set with
  `ZADD <ts> <attempt_id>`, periodic `ZREMRANGEBYSCORE` to trim,
  `ZCARD` to count.
- Threshold: `MAX_FAILURES` failures within `WINDOW_SECONDS`
  triggers lockout for that (IP, username).

### Lockout: per-(IP, username), time-bounded

When threshold exceeded:
- Subsequent requests from that (IP, username) get `429 Too Many
  Requests` with `Retry-After: <seconds>`.
- Lockout duration: `LOCKOUT_SECONDS` (default proposed below).
  Sliding window resets on lockout expiry.
- Lock is automatically released after `LOCKOUT_SECONDS`. No
  manual operator action required for normal recovery.
- Manual operator override: `redis-cli DEL auth_fail:<ip>:<user>`
  clears immediately. Useful if you lock yourself out testing.

**Why per-(IP, username), not just per-IP or per-username:**
- Per-username only: trivial DoS — attacker hits `rescue` 5 times
  from anywhere, locks out the operator.
- Per-IP only: NAT / shared-IP scenarios cause collateral damage
  (legit operator behind same NAT as attacker locked out).
- Per-(IP, username): attacker from one IP can't lock the operator
  out from a different IP. Each (IP, username) bucket independent.

### Counter persistence

Redis. Keys:
- `auth_fail:<ip>:<username>` — sorted set of recent failure
  timestamps, TTL set to `WINDOW_SECONDS + LOCKOUT_SECONDS`.
- `auth_lock:<ip>:<username>` — set when threshold tripped, TTL =
  `LOCKOUT_SECONDS`. Existence of this key = locked.

Why Redis: already in the request hot path; survives container
restart (lockout state shouldn't reset on every deploy); same
infrastructure used everywhere else in the system.

### Logging

Every failed Basic Auth attempt appends to a Redis stream:
`auth_log` — entries `{timestamp, ip, username, status, path,
locked: true|false}`. Capped via `MAXLEN ~ 50000` (matching the
activity log's pattern). Visible:

- **Initial visibility:** a `tools/audit/auth_failures` script
  (small) that pulls recent entries via `XREVRANGE`.
- **Future:** an Admin UI panel showing recent failed attempts
  (out of scope for this FR; possibly Phase 8+).
- **Alerting:** the security_warnings endpoint (added in Phase 5b)
  could surface "N failed admin auth attempts in the last hour"
  as a warning when above some threshold. Optional add.

### Successful auth side effect

A successful Basic Auth from (IP, username) clears any failure
counter for that pair. Rationale: legitimate operator who fat-
fingered their password 3 times, got it right on the 4th, should
not have a stale counter ticking down for the next 15 minutes
that could trip them up later. (The lockout, if already tripped,
does not auto-clear on success — once you're locked you wait.)

## Configuration

Env-var-driven, with sane defaults:

| Var | Default | Notes |
|---|---|---|
| `STRA2US_AUTH_FAIL_WINDOW_SEC` | 900 (15 min) | Sliding window for counting failures |
| `STRA2US_AUTH_FAIL_THRESHOLD` | 5 | Failures within window → lockout |
| `STRA2US_AUTH_LOCKOUT_SEC` | 900 (15 min) | Lockout duration once tripped |
| `STRA2US_AUTH_LOG_MAXLEN` | 50000 | Cap on auth_log stream |

These should be tuned based on observed legitimate-typo rates
(probably very low) vs. attacker-attempt rates (will become
visible once logging is on).

## Out of scope

- **Exponential / escalating lockout** (e.g. second offense → 1h,
  third → 24h). Worth considering if 15-minute lockout proves
  insufficient against persistent attackers; defer until needed.
- **CAPTCHA / interactive challenge.** Doesn't fit the Basic-Auth
  shape; would require a different sign-in surface.
- **IP allowlist / firewall rules.** Operationally separate;
  belongs at the firewall/router layer, not in the application.
  (Worth doing at the firewall layer too if/when convenient.)
- **OAuth flow.** Google handles its own brute-force protection;
  state token is HMAC-signed and unguessable; no application-level
  rate limit needed for `/oauth/*`.

## Implementation outline

1. **New module:** `backend/src/core/auth_throttle.py`
   - `record_failure(ip, username) -> bool` (returns True if now-locked)
   - `is_locked(ip, username) -> Optional[int]` (returns retry_after seconds, or None)
   - `clear_failures(ip, username)` (called on successful auth)
   - `log_failure(ip, username, path)` (writes to `auth_log` stream)

2. **Middleware change:** `admin_auth_middleware` in `main.py`
   - Before verifying Basic Auth: check `is_locked(ip, attempted_username)`. If yes: 429 + `Retry-After`.
   - On verify failure: `record_failure` + `log_failure`.
   - On verify success: `clear_failures`.
   - On no Basic Auth header (cookie path): no lockout logic (lockout is by attempted-username; cookie path has no attempted-username concept).

3. **Audit script:** `tools/audit/auth_failures.sh` reading the
   `auth_log` stream via `redis-cli XREVRANGE`.

4. **Tests:** unit-level on `auth_throttle.py` (window + threshold
   logic without a real Redis), integration-level in
   `tools/tests/test_auth_throttle_live.py`.

5. **Config:** env vars threaded through; defaults in code.

6. **Docs:** README "rescue user" section gains a short note that
   the rescue path now has rate-limiting; pointer to this FR.

## Open questions

- **Should successful auth clear failures across ALL usernames from
  the same IP, or only the matching one?** Current proposal: only
  matching. Alternative: clear all `auth_fail:<ip>:*` on any success
  from that IP (more forgiving). The matching-only choice means a
  legit operator who guessed username `admin` then realized it
  was `rescue` would still have a counter ticking on the wrong
  username. Probably fine; flag for review.

- **How does this interact with the smoke test's auth attempts?**
  Smoke runs with valid creds — should always succeed, no
  failures recorded, no risk of locking out smoke. Worth verifying
  once implemented (a deliberate failed-auth test in the smoke
  suite would be useful).

- **Should the initial threshold be lower for `rescue` specifically?**
  Rescue is the highest-value account; tighter limit (3 failures?)
  could be appropriate. Counter-argument: operator under stress
  during real rescue might fat-finger more than usual. Default
  threshold of 5 is a reasonable compromise.

- **Lockout key includes username — but Basic Auth attempts can
  fail with no username (malformed header).** What to track in
  that case? Proposal: drop malformed Basic Auth on the floor
  (no counter), just log the malformed-attempt for visibility.
  Treat as not-an-auth-attempt rather than a failure.
