# Security audit — 2026-05

Point-in-time security review of the stra2us server, plus the
remediation landed in response. This is a record, not a policy
document; treat it as the authoritative account of what was found,
what was fixed, and what was deliberately left open.

## Scope & method

- **Target:** the FastAPI + Redis backend (`backend/src/`, ~6.6k LOC)
  and its deployment tooling.
- **Method:** four parallel review agents, each owning a dimension —
  (1) auth & access control, (2) input validation & injection,
  (3) web/admin surface (XSS/CSRF/CSP/OAuth), (4) secrets, backup/
  restore, crypto, and config hygiene. Findings cross-validated
  where agents overlapped.
- **Explicitly out of scope:** denial-of-service. The owner already
  treats DoS as a known, accepted risk for this deployment.
- **Threat-model note:** application traffic is not encrypted at the
  app layer; confidentiality on the wire relies on HTTPS / the
  Cloudflare tunnel. Device endpoints are HMAC-signed; admin
  endpoints are htpasswd- or Google-OAuth-gated.

## Summary

Four findings were judged worth acting on; all four were remediated
(one by documentation, by owner decision). No trivial unauthenticated
remote compromise of a correctly-bootstrapped instance was found —
the residual risk clustered in a bootstrap-window credential, cookie/
CSRF hygiene, and a confidentiality weakness in the encrypted-values
feature. A set of lower-priority items was recorded for later. The
codebase was affirmatively sound on most classic surfaces (see
"Verified sound").

| # | Finding | Severity | Response | Status |
|---|---|---|---|---|
| 1 | Bootstrap `rescue` default credential | Medium | Documentation (owner decision) | Done |
| 2 | Cookie flags + CSRF on the admin surface | Medium | Code: cookie flags + Origin guard | Done |
| 3 | kvenc nonce reuse → two-time pad | Medium | Code: per-client monotonic nonce | Done |
| 4 | CORS wildcard + credentials | Medium | Code: pinned origin allowlist | Done |

All changes verified by the test suite (**405 passing**, +16 new:
11 CSRF + 5 kvenc) and, for #3, an end-to-end encrypted-value
decrypt against staging.

---

## Finding 1 — bootstrap `rescue` default credential

**What it is.** `backend/admin.htpasswd.default` ships a tracked
`rescue` htpasswd entry, and `rescue` is hardcoded to full superuser
(`*:rw`, `api/dependencies.py` `RESCUE_USERS` / `_RESCUE_ACL`). The
original audit framed this as "attacker logs in with the known
default."

**Actual exposure (corrected).** The shipped hash is a salted
SHA-256 of a *random* password nobody knows — confirmed by dictionary-
testing ~45 common candidates, zero matches. So it is **not a usable
login**; publishing the hash is safe. The real risk is narrower:
(a) leaving it on the default makes the break-glass path *unusable*
(nobody knows the password); (b) if an operator sets a *weak* rescue
password at bootstrap, that weak password is full superuser, and
there is no Basic-Auth brute-force lockout yet
(`docs/fr_basic_auth_lockout.md`), so it is online-guessable.

**Response (documentation, by owner decision).** This is an AI-first
codebase; the owner chose a noisy, unmissable doc note over fail-
closed code that would be promptly disabled. Notes added at every
spot an installer/agent touches during deploy:
- `backend/admin.htpasswd.default` — ⚠️ header block (the placeholder
  is unusable; rotate to a strong random value before exposure, or
  delete and use OAuth).
- `tools/bootstrap-host.sh` — warning on seed + a MANDATORY rescue-
  rotation step in "Next steps".
- `docs/staging_environment.md` — rotation callout.
- `backend/src/core/admin_auth.py` — strengthened in-code comment
  (superuser + no-lockout consequence).

The existing non-blocking `is_rescue_on_default()` startup + dashboard
warning was kept as-is (it warns, doesn't disable). Verified the
htpasswd parser and rescue-on-default detection still work with the
comment block present.

**Residual.** A weak *operator-chosen* rescue password remains a risk
until the Basic-Auth lockout TODO lands. Mitigation is the
documented "use a strong random value."

---

## Finding 2 — cookie flags + CSRF on the admin surface

**What it is.** Two related gaps:
- The htpasswd/rescue login path (`main.py`) set the `admin_session`
  cookie with only `HttpOnly` — no `Secure`, no `SameSite` — while
  the OAuth path set all three. The highest-privilege session cookie
  was the weaker one (sendable over plain HTTP; weaker vs CSRF).
- No CSRF/Origin check on admin mutation endpoints.

**Exposure.** With the htpasswd cookie lacking `SameSite`, a logged-in
rescue-path admin visiting a malicious page could have state-changing
admin requests (provision, ACL grant, restore) forged against their
session.

**Response (code).**
- Cookie: the htpasswd path now mirrors the OAuth path —
  `Secure` (via the shared `_cookie_secure()`), `SameSite=lax`,
  `path="/"`, `max_age` matching the token's 24h expiry. Importing the
  single `_cookie_secure()` helper prevents the two paths drifting
  apart again.
- CSRF: a defense-in-depth Origin guard in the auth middleware rejects
  state-changing (`POST/PUT/PATCH/DELETE`) requests to the cookie-
  authed admin/app surface whose `Origin` names an unrecognized host,
  returning 403 *before* auth runs. No-Origin requests (curl, the
  stra2us CLI — which carry no ambient cookie) pass through;
  same-origin passes; `STRA2US_ALLOWED_ORIGINS` is the escape hatch.
  `SameSite=lax` is the primary control; this is the second layer.

**Tests.** `backend/tests/test_csrf_origin.py` (11).

---

## Finding 3 — kvenc nonce reuse → two-time pad

**What it is.** The "encrypted values" feature (e.g. a device's wifi
password) encrypts by XOR with a keystream
`HMAC(secret, label || nonce || counter)`. The nonce was the response
wall-clock second and did **not** include the key name, so the
keystream was a function of `(secret, second)` only. Two *different*
encrypted values served to the *same* client in the *same* second
shared a keystream:

```
C1 = P1 ⊕ K
C2 = P2 ⊕ K
C1 ⊕ C2 = P1 ⊕ P2     # keystream cancels — a two-time pad
```

A passive on-path observer who captured both could recover `P1 ⊕ P2`
and crib-drag known-structure plaintext (wifi passwords) into the
values themselves.

**Scope of the weakness (what it is NOT).** Confidentiality only. It
does **not** expose the secret (HMAC is one-way; recovering one
second's keystream never yields the key), does **not** enable forgery
(writes need a valid request signature), and is **not** a replay.
Required conditions, all of: an on-path wiretapper, ≥2 *distinct*
encrypted values, same client, same second. With exactly one
encrypted value in the catalog today, the trigger was **unreachable**
— the fix closed a latent issue before a second encrypted value could
make it live.

**Response (code, server-only).** Because clients derive their decrypt
keystream from the server-supplied `X-Response-Timestamp`, the server
alone owns the nonce — so the fix needed **zero client/firmware
changes**. `routes_device.py` now hands out a strictly-increasing
**per-client** nonce, `max(now, last+1)`, via an atomic Redis Lua
script (`next_kvenc_nonce`). Two encrypted values to one client now
always get distinct nonces → distinct keystreams → nothing to cancel.
The nonce is still emitted in `X-Response-Timestamp` and used for both
the signature and the keystream, so clients verify + decrypt
unchanged; it stays within the client's clock-drift window for any
realistic burst. (The full keystream redesign — folding the key name +
a random salt in — was considered and rejected as overkill: it would
have changed the wire format and required a coordinated firmware
rollout.)

**Tests.** `backend/tests/test_kvenc_nonce.py` (5), including a pair
that demonstrates the original two-time-pad leak and proves distinct
nonces close it, plus a round-trip confirming client-transparency.

**Residual / forward note.** If a second `encrypted: true` key is ever
introduced, this fix already covers it. The cryptographically cleaner
"fold key name into the keystream" remains available if per-key
isolation is ever wanted.

---

## Finding 4 — CORS wildcard + credentials

**What it is.** `main.py` configured `CORSMiddleware` with
`allow_origins=["*"]` **and** `allow_credentials=True`. Because the
spec forbids `Allow-Origin: *` with credentials, Starlette reflects
the request Origin and sets `Allow-Credentials: true` — effectively
"any site may make credentialed reads of this API."

**Exposure.** A logged-in admin visiting an attacker page could, in
principle, have that page read `/api/admin/*` (e.g. the full backup)
cross-origin. **Inert today** because `SameSite=lax` on the session
cookie (now on both auth paths, post-#2) stops the browser attaching
the cookie on a cross-site `fetch()`. The CORS config was a latent
landmine — one `SameSite=None` cookie away from being live — and
simply wrong (the admin UI is same-origin; device/CLI clients ignore
CORS, so nobody needed `*`).

**Response (code).** `allow_origins` is now an explicit allowlist
(`_cors_allowed_origins()`), defaulting to `https://<BROWSER_HOST>`
and extended by the shared `STRA2US_ALLOWED_ORIGINS` env var — the
same allowlist the CSRF guard consumes (one list, two consumers,
tolerant of bare hosts or full origins). `BROWSER_HOST` is per-
environment, so staging auto-pins to the staging host.

---

## Verified sound (do not re-litigate)

The review affirmatively confirmed these are handled correctly. Listed
so a future agent doesn't re-flag settled ground:

- **ACL prefix-matching** — no `critter` → `critterchron` confusion;
  wildcard + parent-prefix semantics correct.
- **HMAC comparisons** use `hmac.compare_digest` (constant-time) for
  signatures, session tokens, and passwords.
- **Secret generation** — `secrets.token_hex(32)`, 256-bit CSPRNG.
- **OAuth** — state-parameter CSRF check, ID-token validation,
  `email_verified` enforced, redirect-URI guarded.
- **msgpack** deserialization paths are guarded.
- **CSP** is enforcing on all routes (`script-src 'self'`,
  `frame-ancestors 'none'`); admin-UI output is HTML-escaped; markdown
  is double-sanitized.
- **Backup/restore** — all six endpoints are superuser-gated; scoped
  admins get 403. Per-app dump filtering excludes cross-app data and
  wildcard admins. `force_overwrite` is gated; per-app restore enforces
  the URL app as the authoritative scope filter.
- **`dump_heartbeats`** is ACL-scoped to the caller's `q/<topic>`.
- **No secrets** are written to perf_log, activity_log, or error logs.
- **Redis is not network-exposed**; `.env`/htpasswd are gitignored;
  OAuth/Cloudflare secrets are env-sourced.
- The kvenc HMAC-keystream **construction** (domain-separation label,
  HMAC one-wayness) correctly defeats the bare-XOR known-plaintext →
  secret-recovery attack; only the *nonce* was weak (Finding 3).

## Open / accepted / deferred

Recorded but not actioned in this pass:

- **Basic-Auth brute-force lockout** — still a TODO
  (`docs/fr_basic_auth_lockout.md`); relevant to Finding 1's residual.
- **Request-signing replay window (~5 min, no nonce cache)** —
  deprioritized: requires on-path TLS bypass to exploit, and the
  signing timestamp window is the existing accepted design.
- **`lookup_device` glob metacharacters** — an authenticated admin can
  probe device-name existence across ACL scopes via a SCAN pattern
  (`routes_app.py`). Low; admin-only.
- **Per-app restore trusts the envelope-supplied ACL** to decide
  client scope — superuser-gated, so defense-in-depth only.
- **"Encrypted at rest" wording is misleading** — kvenc values are
  stored in *cleartext* in Redis; encryption is applied only on the
  device GET response. The comment in `routes_admin.py` (backup
  section) and any UI copy should say "wire-encrypted on fetch; stored
  and backed up in cleartext." Doc fix, not yet made.
- **htpasswd hashing is fast (salted single-round SHA-256)** — salted
  + constant-time-compared, but a fast hash; move remaining
  htpasswd/rescue users to bcrypt/argon2 eventually. Low (file is
  gitignored; auth is narrowing to OAuth + rescue-only).

## Changed files (this remediation)

- `backend/admin.htpasswd.default` — rescue rotation notice (F1)
- `tools/bootstrap-host.sh` — rescue rotation step + seed warning (F1)
- `docs/staging_environment.md` — rescue rotation callout (F1)
- `backend/src/core/admin_auth.py` — strengthened comment (F1)
- `backend/src/main.py` — cookie flags, CSRF Origin guard, CORS pin
  (F2, F4)
- `backend/src/api/routes_device.py` — per-client kvenc nonce (F3)
- `backend/tests/test_csrf_origin.py` — new, 11 tests (F2)
- `backend/tests/test_kvenc_nonce.py` — new, 5 tests (F3)
