# Admin Auth Architecture

Scope: how admin identity is established and how the auth middleware
decides what to do with each incoming request. For *what* an
authenticated admin is then allowed to do — the ACL layer, per-route
gating, superuser role, scoped-admin recipes — see
[`acl_model.md`](acl_model.md).

## The two paths

There are two parallel admin auth paths, served by the same backend
container, distinguished by **request hostname**:

| Hostname | Auth mechanism | Purpose |
|---|---|---|
| `stra2us.austindavid.com` (browser host, CF tunnel, HTTPS) | **OAuth (Google)** with session cookie | Default operator path post-v1.5 |
| `iot.stra2us.austindavid.com` (device host, direct A record, HTTP/8153) | **htpasswd** with session cookie | Rescue / break-glass path |

Both paths land authenticated admins in the same dashboard, with the
same `admin_session` cookie, and the same per-user ACL grants from
the `admin_acls:<user>` Redis namespace. The hostname only controls
*how the operator identifies themselves*; the rest of the system
treats them identically.

The split is by design (see
[`fr_v15_incremental.md`](fr_v15_incremental.md), Phase 4): the
device hostname is HTTP-only because the C++ device clients have
no TLS stack, so it can't sit behind Cloudflare. That same
HTTP/8153 path is the rescue route — if Cloudflare or OAuth is
broken, the operator can still reach the admin UI directly.

## The middleware (`backend/src/main.py::admin_auth_middleware`)

Every request to a path returned `True` by `_path_needs_admin_auth`
goes through:

```
1. Has a valid `admin_session` cookie?       → pass through
2. Has a valid `Authorization: Basic` header? → pass through, set
                                                 admin_session cookie
3. Otherwise:
   - Browser host AND OAuth flag enabled?   → 302 → /oauth/google/login
                                                 with ?next=<original-url>
   - Anything else (device host, unknown host, raw-IP)
                                            → 401 + WWW-Authenticate
                                                  Basic realm="Admin Area"
                                              (htpasswd challenge)
```

The "anything else → htpasswd challenge" branch is **fail-closed**:
unexpected hostnames don't accidentally OAuth-redirect; they get the
conservative auth path. The middleware identifies the browser host
by reading the `X-Forwarded-Host` header (set by Cloudflare); falls
back to `request.url.hostname` for non-tunneled access.

## Session cookies

Both auth paths issue a single shared `admin_session` cookie:
- `path=/`, `httponly`, `samesite=Lax`
- Secure flag set on HTTPS (controlled by `STRA2US_COOKIE_INSECURE`
  for local dev)
- Token is base64-encoded JSON: `{u: <username>, e: <expiry>, s: <hmac>}`
- Signed with `ADMIN_SESSION_SECRET` (env-var; random per-boot if
  unset — fine for single-instance, set explicitly for HA)
- 24-hour TTL on the signature; OAuth-issued cookies set a 7-day
  browser cookie max-age (mismatch is intentional — short signature
  TTL forces re-validation; cookie persistence keeps the browser-side
  state for the convenience window)

## OAuth flow (browser host)

Phase 1–3 of v1.5. Routes in `backend/src/api/routes_oauth.py`,
gated by `STRA2US_GOOGLE_OAUTH_ENABLED`. When the flag is off the
routes return 503 with a clear message; when on:

- `GET /oauth/google/login?next=<url>` — generate CSRF state,
  stash it + the `next` URL in temp cookies, 302 to Google.
- `GET /oauth/google/callback?code=&state=` — verify state cookie,
  exchange code for ID token, validate the token (signature,
  issuer, audience, expiry), look up the email in
  `admin_acls:<email>`. If authorized: issue `admin_session` cookie
  + redirect to `next` (validated as same-origin). If not:
  friendly unauthorized landing.

The `next` round-trip preserves the originally-requested URL across
the Google round-trip, so a deep link to `/admin/foo?bar=baz`
survives sign-in.

## htpasswd flow (device host)

Pre-v1.5 mechanism, retained as the rescue path.

- `backend/admin.htpasswd` — flat file, format
  `username:salt$sha256(salt+password)`. *Not* Apache's standard
  htpasswd format; written by `backend/create_admin.py`.
- `verify_password` reads the file fresh on each request (no
  in-memory cache), splits salt/hash, recomputes, constant-time
  compares.
- On success, the middleware issues an `admin_session` cookie just
  like OAuth does, so the user doesn't get re-prompted on every
  request.

## RESCUE_USERS — break-glass ACL

Defined in `backend/src/api/dependencies.py`. A small set of
usernames (default `{"rescue"}`, env-overridable via
`STRA2US_RESCUE_USERS`) get a hardcoded wildcard ACL when no
`admin_acls:<user>` Redis row exists. This means:

- A `rescue` user who can authenticate via htpasswd always has full
  permissions, regardless of whether the ACL row was provisioned
  or whether Redis state is intact.
- Without this, a fresh-bootstrap host or a corrupted Redis would
  leave you with "I can sign in as rescue but I can't do anything"
  — a useless rescue path.

## The `rescue` user's bootstrap-default password

Shipped in `backend/admin.htpasswd.default`. Copied into
`backend/admin.htpasswd` by `tools/bootstrap-host.sh` when no live
htpasswd exists yet (on existing hosts, the merge logic adds the
`rescue` line if it's missing without disturbing other entries).

Operator is expected to override the default password before
exposing the device hostname to anything sensitive. Two
notification mechanisms surface that obligation:

- **Soft warning** — `is_rescue_on_default()` runs at server import
  time and prints a bordered warning to stdout (visible via
  `docker logs` since supervisord redirects program output to
  `/dev/stdout` and `/dev/stderr`).
- **Loud banner** — `/api/admin/security_warnings` returns a
  warning record when the live htpasswd's `rescue` line matches
  the default file byte-for-byte; the admin UI fetches this on
  page load and renders an amber banner across the top.

The check is intentionally byte-for-byte. Once the operator runs
`create_admin.py rescue '<newpass>'`, a fresh salt is generated
and the lines diverge — the warning silences. Salt-based identity
is what makes the check tractable without exposing the default
password to the running code; the tradeoff is documented in
[`README.md`](../README.md) ("the rescue user").

## Logout

`GET /admin/logout` (carved out of `_path_needs_admin_auth`):
clears `admin_session`, `oauth_state`, `oauth_redirect_to`
cookies. Behavior splits by hostname:

- **Browser host:** 200 with a clean HTML page ("Signed out. Sign
  in again."). No Basic Auth dialog.
- **Device host:** 401 with `WWW-Authenticate: Basic
  realm="logged-out"` (different from the live realm
  `"Admin Area"`). The realm change is what flushes Chrome's
  cached Basic Auth credentials — the only way to "log out" of
  Basic Auth without quitting the browser entirely. Browser may
  briefly flash the Basic Auth dialog; closing it leaves the user
  cleanly logged out.

A "Sign out" link in the admin UI sidebar navigates here.

## What's gated, what isn't

`_path_needs_admin_auth` returns True for:
- `/admin*` (the dashboard)
- `/api/admin*` (the dashboard's APIs)
- `/app/<app>/<device>/...` (per-customer device views,
  see `fr_application_view.md`)

Returns False (public) for:
- `/oauth/*` (must be reachable without a session — that's the point)
- `/admin/logout` (must work even with a corrupted session)
- `/app` and `/app/` (bare landing form, lookup-by-device)
- `/app/_static/*` (public assets — `_`-prefixed reserved-namespace
  convention)
- `/api/app/*` (public lookup endpoints)
- Device routes: `/q/`, `/kv/`, `/firmware/` — gated by HMAC
  signature, not session auth (see `acl_model.md`)
- `/health`, `/`

## See also

- [`fr_v15_incremental.md`](fr_v15_incremental.md) — phase-by-phase
  context for how this evolved, including the rollback plan and
  rescue-path tradeoffs.
- [`acl_model.md`](acl_model.md) — what an authenticated admin is
  then allowed to do.
- [`fr_application_view.md`](fr_application_view.md) — how the
  per-customer `/app/<app>/<device>/...` paths integrate.
