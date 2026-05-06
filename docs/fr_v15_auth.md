# FR: v1.5 — Customer-facing auth, provisioning, multi-device UX

*Drafted 2026-05-04 — design for review, not yet implemented. Captures
the v1.5 scope as agreed during planning. Builds on
[`fr_application_view.md`](fr_application_view.md) (which shipped the
customer surface; v1.5 makes it real for actual external customers).*

## Status

**Pending — not yet implemented.** This is the v1.5 starting point;
treat the scope below as committed-but-revisable rather than locked.

## Problem

`fr_application_view.md` shipped a customer-facing `/app/<app>/<device>`
surface, but the auth + provisioning layer is "trusted-internal-staff-
with-tooling-access" UX:

- Sign-in is a browser basic-auth dialog (jarring for an external
  customer).
- No logout link; closing all browser windows is the workaround.
- Customer accounts are provisioned via `create_admin.py` +
  `redis-cli SET 'admin_acls:<user>' ...` — the operator has to
  hand-craft a 3-perm ACL JSON. This footgunned twice during the
  application-view rollout (operator forgot one of the three perms;
  customer hit confusing 403s).
- A customer with multiple devices needs separate bookmarks; no
  device-list landing or in-page switcher.
- The `/app/` landing form's `lookup_device` endpoint is wide-open
  to anyone on the internet — fine while VPN-gated, not fine when the
  surface is real-internet-facing.

v1.5 makes the surface real: real auth (Google OAuth), real
provisioning (form, no ACL JSON), real multi-device UX (landing list +
switcher), real public-internet hardening (Turnstile).

## Design property: one auth path for everyone, one rescue path for emergencies

**Google OAuth is the universal auth path.** Customers, internal
staff (operators, support), all of it. No domain allowlist — any
Google account is accepted; authorization is purely "do you have an
`admin_acls:<email>` row?" Unauthorized customers see a friendly
"contact your administrator" page.

**One `rescue` htpasswd entry is the universal fallback.** Used for:
- Production emergency access (Google or stra2us auth machinery
  broken, need to get in to fix it)
- Local development (no OAuth ceremony in dev)
- CI / automated tests (no interactive browser flow needed)

The basic-auth path is reachable *only* for usernames in the
`RESCUE_USERS` allowlist. Any other htpasswd entry is rejected (so
nobody can mint a parallel non-Google admin account by editing
htpasswd; that's reserved for `rescue` only).

**Why a single rescue mechanism instead of multiple bypass paths:**
during planning we considered (a) an `STRA2US_DEV_AUTH_BYPASS` env
var for local dev, (b) a service-account / API-token mechanism for
non-interactive scripts, (c) a separate test-only mock OAuth callback
for tests. All three collapsed into "just use the rescue user." Saves
~70 LOC and three attack surfaces.

## Auth flow

### Browser flow (interactive)

1. Customer hits **anywhere under `/app/` or `/admin/`** — including
   the bare `/app/`. **There is no pre-auth surface in v1.5.** The
   v1 lookup form (which existed because customers needed a way to
   resolve "I don't know my URL" without a session) is gone — Google
   sign-in dissolves that chicken-and-egg, since the post-auth
   landing can derive the customer's devices from their ACL.
2. No `admin_session` cookie, no rescue basic-auth → middleware
   sets a short-lived `oauth_redirect_to` cookie (path=/, httponly,
   secure, 10min TTL) holding the original URL, then redirects to
   `/oauth/google/login`.
3. `/oauth/google/login` generates a fresh random state token, sets
   it in a separate `oauth_state` cookie (also path=/, httponly,
   secure, 10min TTL), constructs the Google authorize URL with our
   client_id, redirect URI, scope (`openid email`), state=<token>,
   and 302s the browser to it.
4. Google's consent screen → user signs in / picks an account.
5. Google 302s back to `/oauth/google/callback?code=...&state=...`.
6. Stra2us:
   - Reads `oauth_state` cookie, compares to `state` query param
     (CSRF defense). Mismatch → 400. Clears the cookie either way.
   - Exchanges the code for an ID token via Google's token endpoint.
   - Validates the ID token via the `google-auth` library: signature
     against Google's JWKS, issuer == `accounts.google.com`,
     audience == our client_id, expiration in the future.
   - Extracts `email` from the verified token claims.
7. Looks up `admin_acls:<email>` in Redis.
   - **Found**: issues `admin_session` cookie with `<email>` as the
     identity (path=`/`, httponly, secure-in-prod, 7-day TTL). Reads
     `oauth_redirect_to` cookie for the original URL, clears it,
     302s the browser there.
   - **Not found**: clears the redirect cookie, renders
     `/oauth/unauthorized` page (see Known Issues for copy).

**State + redirect-back are stored as separate cookies, not in the
OAuth `state` parameter itself.** This decouples CSRF defense (which
the state param is for) from redirect-tracking (which has size and
encoding pitfalls when stuffed into state). Two small cookies, each
with one job.

**Cookie path is `/` for `admin_session`, `oauth_state`, and
`oauth_redirect_to`.** Default `path=` would be the request path of
whichever endpoint set it (`/oauth/google/callback` for the session
cookie), which would silently fail to apply across `/app/` and
`/admin/`. Easy to miss, expensive when missed.

### Rescue path (basic-auth, allowlisted username only)

1. Request includes `Authorization: Basic ...` header.
2. Middleware decodes; checks username is in `RESCUE_USERS = {"rescue"}`.
3. If not → fall through to OAuth redirect (as if no header).
4. If yes → run the existing `verify_password` against the htpasswd
   file. If valid, set the session cookie with `rescue` as the
   identity, proceed.

**The rescue user's `*:rw` ACL is hardcoded in code, NOT stored in
Redis.** Specifically: `load_admin_acl()` short-circuits when the
username is in `RESCUE_USERS` and returns
`{"permissions":[{"prefix":"*","access":"rw"}]}` without touching
Redis. This is load-bearing: a Redis outage is exactly when you
need rescue access, and storing the rescue ACL in Redis would
defeat the purpose. ~5 LOC at the top of `load_admin_acl`.

Activity log entries from rescue access are clearly attributable
(`client_id: "rescue"`).

### Session

- Cookie: `admin_session`, httponly, secure (in prod), 7-day
  expiration.
- Refresh-on-activity: each request that successfully validates the
  cookie re-issues it with a 7-day TTL. Inactive customers get
  re-auth-prompted after 7 days; active ones stay signed in
  indefinitely.
- Logout: clears the `admin_session` cookie. Does NOT log the user
  out of Google (re-auth is one click; revoking the Google
  app-grant feels punitive for a "step away from the screen" use
  case).

### Activity logging

`client_id` field in activity-log entries becomes the email (or
`rescue` for rescue access). Existing log filtering / display works
unchanged — slightly wider column. No migration of historic entries.

## Customer + admin provisioning UI

Replaces the manual `create_admin.py` + `redis-cli SET 'admin_acls:...'`
workflow with a single form on the admin's "Admin Users" tab.

**Two form variants** (or one with a mode toggle):

**"Add a Customer for App"** — for the customer-shape ACL.
- Inputs: `email` (Google), `app` (dropdown of catalogs the operator
  has access to), `device(s)` (multi-select dropdown of devices in
  that app, populated from `/api/admin/catalog/{app}/devices`).
- **Provisioning order is fixed:** devices must be provisioned (via
  `provision_device` or the bare `Register New Client` form) *before*
  the customer who owns them. The device-multi-select dropdown only
  shows devices that already exist; you can't pre-create a customer
  account for a device that hasn't been registered yet. Operationally
  this matches the real-world flow (devices are physical hardware
  that gets provisioned at manufacture; customers come later when
  the device ships).
- Submit: server creates `admin_acls:<email>` with the canonical
  shape, multi-device case adds an extra `<app>/<device>:rw` per
  selected device:
  ```json
  {"permissions": [
    {"prefix": "<app>/<device1>",  "access": "rw"},
    {"prefix": "<app>/<device2>",  "access": "rw"},
    {"prefix": "<app>/public",     "access": "r"},
    {"prefix": "_catalog/<app>",   "access": "r"}
  ]}
  ```
- Returns: confirmation + the URL to share with the customer
  (`/app/<app>/<device1>` for single-device; `/app/<app>` landing
  for multi).
- Idempotent on existing email (mirrors `provision_device`'s
  semantics): replaces the ACL wholesale, leaves any session cookies
  intact.

**"Add Admin"** — for internal staff with broader scope.
- Inputs: `email` field + the existing per-row AclUpdate editor
  (add/remove rows of `{prefix, access}`).
- Submit: write to `admin_acls:<email>`.
- **Honest scope:** this is *not* a footgun-eliminator the way "Add
  a Customer" is — it's the existing free-form ACL editor with an
  email field stuck on. Scoped admins (e.g. "admin for one app")
  still have to hand-craft the ACL. The customer flow gets the
  canonical-shape generator because that's the dominant case;
  admin shapes vary too much to template usefully. If a future
  scoped-admin shape becomes common (e.g. per-app operators), add
  a "Scoped Admin for App" template alongside.

Both eliminate the 3-perm-ACL-JSON-by-hand step *for customers*. The
admin form is just convenience packaging for the existing editor.

## Customer landing + multi-device UX

With the v1 lookup form removed, the **customer landing IS the
device list** — there's no other entry point. Confirmed v1.5 scope
since the operator owns several critterchron devices personally and
external customers can be expected to too.

**`/app/` landing (post-auth).** Shows every device the customer has
rw access to, across all apps in their ACL. Derived from the
caller's ACL — every two-segment `<app>/<device>:rw` perm becomes a
card or row. Each row links to `/app/<app>/<device>/`. Includes the
same "Online / Recently active / Offline" status badges as the
per-device pages, so the landing also serves as a fleet-status
overview.

**`/app/<app>/` landing (post-auth).** Same shape, scoped to one
app — useful for customers with devices in multiple apps who want
to focus. Reachable as a deep link; the bare `/app/` lists across
apps and also links into per-app subsets.

**Header switcher on `/app/<app>/<device>`.** Dropdown next to the
device name, listing the customer's other devices in the same app.
One-click switch. Sticky across pages so the customer can quickly
flip between e.g. ricky_raccoon and steel_hamster.

**Single-device customer:** the bare `/app/` landing immediately
302s to their one device (no need to land on a list of one). The
header switcher just doesn't render.

**Stale bookmarks (devices the customer doesn't own).** When a
signed-in customer hits `/app/<app>/<unknown_device>`, the ACL
check fails → 302 to `/app/<app>/` (their device list for that
app). No "device not found" terminus; just bounces them to where
their devices actually are.

**Cross-app:** the bare `/app/` lists devices across apps. Per-app
deep linking (`/app/<app>/`) is the focused subset. If we ever
need a per-app "switch app" header on top of the per-device pages,
add it later — not required for v1.5.

## Public-internet hardening: Turnstile

**Hard requirement for v1.5, narrow scope.** Cloudflare Turnstile
(or equivalent) gates the **single pre-auth surface that remains**
after the lookup-form removal: the OAuth kickoff page.

- `/oauth/google/login` — Turnstile widget rendered on the page;
  server validates the response token via Cloudflare's `siteverify`
  endpoint before redirecting to Google. Defends against
  programmatic credential-stuffing-style attacks against the OAuth
  flow. Belt-and-suspenders since Google itself rate-limits OAuth
  attempts heavily, but worth keeping for the principle.

**Behind OAuth doesn't need it.** `/app/<app>/<device>` and friends
require a valid session cookie; Turnstile would just be friction.

**Mode: non-interactive.** Cloudflare runs heuristics quietly in
the background; the user never sees a challenge UI. This is
*measurably weaker* than managed mode in the abstract (Cloudflare-
flagged-suspicious bots get tokens issued anyway), but in our
specific context the practical weakening is small: Google + ACL is
the real auth boundary, and the OAuth kickoff is itself behind
Google's anti-bot machinery. Trade is zero customer friction, ever.
Mode is a Cloudflare-dashboard config (against the site key, not
in stra2us code) — flippable to managed later via a click if real
abuse shows up, no deploy needed.

**Failure mode: fail closed.** If Cloudflare's `siteverify`
endpoint times out or returns garbage, the OAuth kickoff rejects
the request with a "verification temporarily unavailable, try
again in a moment" message. Cloudflare's siteverify uptime is good
enough that this rarely fires; bots-getting-through during an
outage feels worse than legitimate-traffic-rejected for the same
window. Rescue user (basic auth) still works during a Turnstile
outage — staff aren't locked out.

**Site key + secret key management:**
- `STRA2US_TURNSTILE_SITE_KEY` (env var, public, embedded in HTML)
- `STRA2US_TURNSTILE_SECRET` (env var, server-side only)
- Cloudflare provides always-pass test keys for dev/CI:
  `1x00000000000000000000AA` site key + `1x0000000000000000000000000000000AA`
  secret. Local dev + tests just use those — no special-case code
  in stra2us, the widget renders and the verify call always
  succeeds.

**No in-process rate limiting in v1.5.** Cloudflare's edge rate
limits + Turnstile together cover most of the threat surface. If
real abuse surfaces a need (e.g. someone hitting origin directly
via a bypass), file a follow-up — not blocking ship.

## Migration

There's exactly one user today (the operator). So the migration is
trivial:

1. **Deploy v1.5 with both auth paths active.** Existing htpasswd
   user keeps working; Google OAuth also works for anyone
   provisioned.
2. **Provision the operator's Google email.** Create
   `admin_acls:<operator>@<gmail-domain>` with `*:rw`. Sign in via
   Google once, confirm it works.
3. **Drop non-rescue htpasswd entries.** Keep only `rescue`. Anyone
   trying to sign in with the old htpasswd username + password will
   fail (not in `RESCUE_USERS`, won't reach `verify_password`).

Total operator-side work: register one Google OAuth app, provision
one Google account, one verification login. Done.

**Legacy `austin` data** (existing `admin_acls:austin` row, historic
activity log entries with `client_id: austin`) — leave intact for
historical reference; new ACL goes under the email. Operator can
clean up later when convenient.

## Rescue mechanism — implementation detail

```python
RESCUE_USERS = {"rescue"}

def authenticate_request(request):
    # 1. Cookie wins
    cookie = request.cookies.get("admin_session")
    if cookie:
        user = verify_session_token(cookie)
        if user:
            return user

    # 2. Basic auth, allowlisted to rescue users only
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            username, password = decode_basic(auth)
            if username in RESCUE_USERS and verify_password(username, password):
                return username
        except Exception:
            pass

    # 3. No valid auth → caller is responsible for redirect-to-OAuth
    return None
```

Tests that today use `STRA2US_ADMIN_USER=admin STRA2US_ADMIN_PASS=...`
will use `STRA2US_ADMIN_USER=rescue STRA2US_ADMIN_PASS=...` — single
character change to the env vars, scaffolding unchanged.

## Implementation phases

**Phase 1 — Google OAuth scaffolding.** Register OAuth app, store
client_id + secret in env vars. Add `/oauth/google/login` and
`/oauth/google/callback` routes. Validate ID tokens via `google-auth`
lib. Cookie issuance unchanged from current. Feature-flagged off by
default; opt in via env var. ~200 LOC.

**Operational checklist for Phase 1** (easy to forget, expensive to
discover late):

*Google Cloud Console*
- Create a new project (or reuse an existing one) at
  https://console.cloud.google.com/ — top-of-page project selector.
- APIs & Services → OAuth consent screen. Pick "External" (we want
  any Google account to be able to attempt sign-in; admin_acls
  filtering happens server-side on the callback). Fill in the app
  name + support email; the rest is informational and can stay
  minimal for now. Publishing status can stay "Testing" for the
  initial dev/staging exercise but must move to "In production"
  before Phase 2 cuts over (Testing mode caps at 100 users +
  expires refresh tokens after 7 days — we don't use refresh tokens
  but the user cap will bite at scale).
- APIs & Services → Credentials → Create Credentials → OAuth client
  ID → Web application.
- Add ALL redirect URIs the deployment uses, e.g.
  `https://prod.example.com/oauth/google/callback`,
  `https://staging.example.com/oauth/google/callback`,
  `http://localhost:8153/oauth/google/callback` (for local dev).
  Forgetting one means OAuth fails on that environment with a
  Google-side `redirect_uri_mismatch` error that's confusing if
  you're not looking for it.
- Pin scope to `openid email` (NOT `offline_access` — we don't need
  refresh tokens, our 7-day cookie + refresh-on-activity covers it).
  Code already requests this; nothing to do in the Console.

*Server env vars* (read by `core.oauth`; missing-when-enabled raises
a clear RuntimeError so the failure mode is obvious at startup)
- `STRA2US_GOOGLE_OAUTH_ENABLED=1` — feature flag. Leave unset on
  prod until staging e2e is signed off; setting to `1` is the entire
  switch that activates the routes (they always register but 503
  until enabled — easier to debug than silent 404s).
- `STRA2US_GOOGLE_CLIENT_ID=<...>.apps.googleusercontent.com` —
  the public client ID from the Console.
- `STRA2US_GOOGLE_CLIENT_SECRET=<secret>` — keep out of the repo;
  inject via container/env config.
- `STRA2US_OAUTH_REDIRECT_URI=https://<host>/oauth/google/callback`
  — must EXACTLY match one of the URIs registered in the Console
  (scheme, host, port, path all checked by Google).
- `STRA2US_COOKIE_INSECURE=1` — escape hatch for local dev over
  plain HTTP. Production must leave this UNSET so cookies get the
  `Secure` flag.

*Verification before Phase 2*
- Phase 1 ships dormant. To exercise it on staging:
  1. Set the four env vars above on the staging container.
  2. Provision your own Google email in Redis:
     `redis-cli SET 'admin_acls:<you>@gmail.com' '{"permissions":[{"prefix":"*","access":"rw"}]}'`
     This is the same JSON shape `routes_admin.set_admin_acl` writes
     and `dependencies.check_acl` reads — `permissions` is a list of
     `{prefix, access}` objects; `prefix: "*"` is the wildcard that
     matches every namespace; `access: "rw"` is read+write. (Common
     pitfall: the FR was originally drafted with a hand-wavy
     `{"acls": ["*:rw"]}` string form that the code does NOT
     understand — anything not matching the `permissions` shape
     evaluates as zero perms and you'll be locked out of every
     resource despite the OAuth flow letting you "in.")
  3. Visit `https://staging.example.com/oauth/google/login` directly
     in a fresh browser session (no `admin_session` cookie set).
  4. Confirm the round-trip lands you back in `/admin/` signed in
     as your Google email — check the Admin Users page reflects the
     identity you logged in with.
- Also confirm the unauthorized branch: open an incognito window,
  sign in with a Google account that doesn't have an `admin_acls:`
  row, confirm you land on `/oauth/unauthorized` (403) without an
  `admin_session` cookie set.
- Run the Layer 1 unit tests locally before deploy:
  `cd backend && ./venv/bin/python -m pytest tests/test_oauth.py -v`.
  17 tests, all hermetic (no network), should be green in <1s.

*Code touch-points* (so Phase 2 has the map)
- `backend/src/core/oauth.py` — config loaders, state-token CSRF
  helpers, authorize-URL builder, token exchange, ID-token validation.
- `backend/src/api/routes_oauth.py` — three routes (`/oauth/google/
  login`, `/oauth/google/callback`, `/oauth/unauthorized`) +
  unauthorized/error HTML pages.
- `backend/src/main.py` — router registration, plus the carve-out in
  `_path_needs_admin_auth` that lets `/oauth/*` paths through the
  middleware unauthenticated (the OAuth flow is its own auth).
- `backend/tests/test_oauth.py` — 17 unit tests with mocked Google.

**Phase 2 — Rescue path + middleware rewrite.** Replace the existing
admin-auth middleware with the dual-path version above. Restrict
basic auth to `RESCUE_USERS`. Migrate operator's account to Google.
Drop non-rescue htpasswd entries. ~50 LOC + operational migration.

**Phase 2 risk + rollback plan.** Phase 2 is the irreversible cutover
moment — if it ships with a bug (redirect loop, session validation
broken, cookie scope issue, etc.), every non-rescue user is locked
out at once.

- **Verify on a non-prod environment first.** Sign in via Google end-
  to-end on staging before flipping prod.
- **Have a rollback path ready.** Either: keep the old middleware
  code behind a flag (`STRA2US_AUTH_LEGACY=1` falls back to the
  pre-Phase-2 dual-path-everyone-allowed mode) for the first week
  post-deploy. Or: pre-stage a "git revert this commit" PR that
  restores the old middleware so revert is one merge away.
- **Rescue access is the absolute fallback.** Rescue htpasswd entry
  must be confirmed working *before* Phase 2 ships. Test it
  explicitly during Phase 1 verification — sign in as `rescue` via
  basic auth, confirm cookie + session work.

**Phase 3 — Provisioning UI.** New "Add a Customer" / "Add Admin"
forms on the Admin Users tab, mirroring `provision_device` but for
admin user ACLs. Eliminates the 3-perm footgun. ~80 LOC server +
~50 LOC client.

**Phase 4 — Multi-device customer UX.** `/app/<app>` landing with
device list (derived from caller's ACL); header switcher on
per-device pages. Empty-list / single-device cases handled. ~150 LOC.

**Phase 5 — Logout link.** Cookie-clear endpoint + UI links in both
`/app` and `/admin` headers. ~20 LOC.

**Phase 6 — Turnstile integration.** Add Turnstile site-key to env;
include the Turnstile widget (non-interactive mode) on
`/oauth/google/login` page; verify the response token server-side
via Cloudflare's `siteverify` endpoint before redirecting to Google.
Fail-closed on verify failure. ~30 LOC client + ~30 LOC server.

**Phase 7 — Remove v1 lookup form + cleanup.** Two cleanups bundled
since they're both about the now-vestigial v1 surfaces:
- Delete `landing.html`, the `lookup_device` endpoint, the form's
  JS handler, the `/app/` static-mount entry for the form. The
  `/app/` route handler now just redirects to OAuth (or to the
  device-list landing if already signed in). ~80 LOC removed.
- Delete the v1 basic-auth-for-everyone middleware path,
  `create_admin.py` if fully retired, etc. ~50 LOC removed.

Phases roughly orderable as listed; 1+2 must come before 3-7. 4 and 5
can land in either order. 6 can land any time after 1. Phase 7
must come *after* Phase 4 (multi-device landing exists) so the
lookup form's removal doesn't strand customers without a way to
reach their devices.

**Total: ~500-700 LOC net, ~2 weeks for one focused developer
including verification.** (Slightly less than the prior estimate
because the lookup-form removal cancels out some of the new code.)

## OAuth test plan

OAuth flows have a notorious "works locally, breaks on staging because
of cookies/CORS/redirect-URI weirdness" pattern. Three layers of
testing, each catching a different family of bug.

### Layer 1: server unit tests (no real Google round-trip)

Mock the ID-token validation function (return a synthetic claims
dict with `email` set). Test the callback handler's branching logic
in isolation:

- **Authorized email path.** Mock returns `{email: "alice@example.com"}`,
  `admin_acls:alice@example.com` exists in Redis →
  `admin_session` cookie issued, redirect to `oauth_redirect_to`
  destination, both temp cookies cleared.
- **Unauthorized email path.** Mock returns
  `{email: "stranger@example.com"}`, no Redis row →
  `/oauth/unauthorized` rendered, no `admin_session` cookie set.
- **State token mismatch.** Callback called with `state=foo` but
  `oauth_state` cookie holds `bar` → 400, no token exchange
  attempted.
- **State token absent.** Callback called with no `oauth_state`
  cookie → 400.
- **Token validation failure.** Mock raises (invalid signature, wrong
  audience, expired) → 401, no cookie set.
- **Cookie path correctness.** Inspect the `Set-Cookie` headers in
  the response: `admin_session` must have `Path=/`, not the request
  path. (This is the silent-break risk if missed.)
- **Redirect-back URL preserved.** Set `oauth_redirect_to` to
  `/app/critterchron/ricky_raccoon`, complete callback → response
  is a 302 to that URL.

### Layer 2: live integration tests (real server, mocked Google endpoint)

Run uvicorn locally; use a mocked-Google server (a pytest fixture
that exposes `/token` and `/jwks` endpoints returning canned
responses). Drive the full redirect chain via `requests` with a
`Session` so cookies persist across hops:

- **Happy-path round-trip.** GET `/admin/` → 302 to `/oauth/google/login`
  → 302 to mocked-Google `/authorize` → mocked-Google 302s back to
  `/oauth/google/callback?code=fake&state=<stored>` → `/admin/` (the
  original URL) finally returns 200 with the page content.
- **Cookie scope across surfaces.** After signing in via the
  authorize-callback chain, GET `/admin/api/admin/me` and
  `/app/critterchron/ricky_raccoon` both succeed without re-auth
  (proves the session cookie applies across both path namespaces).
- **Cookie expiry / refresh-on-activity.** Time-travel the
  session-cookie expiration (mock the cookie's `exp` claim or use
  `freezegun`); verify a request just before expiry refreshes the
  cookie, a request just after expiry redirects to OAuth.
- **Rescue path.** `requests.get(url, auth=("rescue", pw))` →
  works, `client_id` in any subsequent activity log entry is
  `rescue`. `requests.get(url, auth=("austin@example.com", pw))` →
  rejected (basic auth allowlist denies non-rescue usernames).
- **CSRF mismatch end-to-end.** Manually fabricate a callback URL
  with a state value that doesn't match any active `oauth_state`
  cookie → 400.

### Layer 3: real-Google smoke tests (manual, pre-deploy)

A small checklist to run by hand on staging before flipping Phase 2
to prod. Real Google round-trip; can't be automated without a
service account + headless browser, which is its own can of worms.

- **Operator's actual Google account signs in.** Provision the email
  in `admin_acls:`; verify sign-in lands on the original target URL.
- **Unprovisioned Google account is rejected friendly.** Sign in
  with a different Google account (one without an ACL row); verify
  the unauthorized page renders, no broken state.
- **Cookie persists across `/app/` and `/admin/`.** After signing in
  on `/admin/`, navigate to `/app/<app>/<device>/` in the same
  browser tab; verify no re-auth.
- **Sign out works.** Click logout in either header; verify the
  cookie is cleared (DevTools → Application → Cookies); a refresh
  redirects to OAuth.
- **Rescue path on staging.** Confirm rescue htpasswd basic-auth
  works on staging *before* Phase 2 prod cutover. Non-negotiable —
  rescue is the only fallback if Phase 2 has a regression.

### Test-time effort

- Layer 1: ~150 LOC + fixtures, one-time.
- Layer 2: ~200 LOC + a small mock-Google fixture, one-time. Hardest
  layer to get right; budget 2-3 days of debugging.
- Layer 3: ~30 minutes per pre-deploy.

The Phase 2 cutover gates on Layers 1 and 2 passing in CI, and Layer
3 having been exercised on staging. Phase 1 (OAuth scaffolding behind
a feature flag) can ship with just Layer 1.

## Decisions locked during planning

| Decision | Choice | Rationale |
|---|---|---|
| Auth backend for customers + staff | Google OAuth | One pattern, no passwords to manage |
| Domain allowlist | None — any Google account | Simpler; ACL row is the gate |
| Email-sending infrastructure | Not needed | Google handles all customer comms |
| Password reset flow | Not needed | Google handles it |
| Self-signup | Not needed | Operator provisions OOB-known emails |
| Session timeout | 7 days, refresh-on-activity | Re-auth via Google is friction-free |
| OAuth scope | `openid email` (minimal) | Just enough to look up the ACL |
| Logout behavior | Clear cookie only | Don't log out of Google itself |
| Multi-device UX | In scope; **landing IS the device list** | Operator owns several devices; real need; with Google sign-in there's no other entry point |
| v1 lookup form (`landing.html` + `/api/app/lookup_device`) | **Removed** | Vestigial once Google sign-in is universal — customer doesn't need to "look up" their device, post-auth landing shows their list |
| Turnstile | Hard requirement, **single surface** | Only pre-auth surface left is `/oauth/google/login` |
| Turnstile mode | Non-interactive | Zero customer friction; Google + ACL is the real defense; flippable to managed via Cloudflare dashboard if abuse appears |
| Turnstile failure mode | Fail closed | Cloudflare uptime is good; rescue user covers staff during a Turnstile outage |
| In-process rate limiting | None in v1.5 | Cloudflare edge + Turnstile cover the threat surface |
| Rescue mechanism | Single htpasswd entry, hardcoded `*:rw` ACL | Universal fallback (prod + dev + CI); ACL must NOT live in Redis or rescue is useless during a Redis outage |

## Known issues / explicit caveats

- **Google reachability is now a runtime dependency.** Today if Google
  is down, nobody can sign in via OAuth. Rescue htpasswd is the
  fallback. Operator awareness: monitor Google's status; if a real
  outage happens, switch to rescue.

- **Single rescue user is a single point of failure.** If the rescue
  password is lost or the htpasswd file is corrupted, recovery
  requires direct Redis + filesystem access. Mitigate by keeping the
  rescue password in a password manager, not in head/sticky-notes.

- **Activity log identity drift.** Historic entries show pre-migration
  usernames (`austin`); post-migration entries show emails. Operator
  reading old logs needs to know "austin and austin@google.com are
  the same person." No automated unification — out of scope.

- **No service-account / non-interactive admin path.** If a future
  bulk-provisioning script wants to call `/api/admin/...`, it uses
  the rescue credentials. If that becomes uncomfortable (e.g.,
  rotating rescue creds breaks the script), file a follow-up for a
  proper service-account / API-token mechanism.

- **OAuth callback security depends on the state token + redirect URI
  allowlist on Google's side.** Standard OIDC hygiene; the
  `google-auth` lib handles validation. Operator just has to keep
  the redirect URI list in Google Cloud Console synced with the
  prod/staging URLs.

- **No self-service request flow for unauthorized users.** A random
  Google user who signs in without a provisioned ACL row lands on
  `/oauth/unauthorized` and is told to contact their administrator
  out-of-band. **There is intentionally no "request access" button,
  no email-the-operator form, no comment-attach mechanism.** The whole
  v1.5 model presumes prior allocation: someone (e-store, sales
  channel, ops handoff) collects the customer's Google email at
  acquisition time, that email gets provisioned via the admin UI
  before the customer ever arrives. The unauthorized page is the
  "you're in the wrong place" terminus, not a self-onboarding
  funnel. If/when self-service onboarding becomes a real ask, file
  a separate FR — it's a different shape than v1.5.

## What this FR is *not* proposing

- **Federated auth beyond Google.** No SSO, OAuth-via-other-providers,
  enterprise SAML, etc. Pure Google OAuth. v2 if customer demand
  surfaces.
- **Self-signup / customer-self-claim.** Operator provisions emails
  out-of-band. No "sign in and ask to be added" flow.
- **Notification emails.** No "your device is offline" emails, no
  password-changed notifications, no welcome emails. Stra2us never
  sends mail.
- **Multi-app customer UX.** A customer with devices in multiple apps
  still uses separate `/app/<app>/...` URLs per app. v2 if needed.
- **Mobile responsive pass on `/app`.** Deferred per
  fr_application_view.md known issues.
- **Per-app branding** (catalog-driven `brand_color` etc.). Deferred
  (P2 in admin_ui_todo).
- **Activity Logs UI dropdown / pagination.** Deferred (P3 in
  admin_ui_todo).
- **`peek_kv` hardening for encrypted records.** Defense-in-depth
  filed in fr_application_view.md "Longer-term"; not blocked by
  v1.5.
- **Auth backend swap from htpasswd to Postgres-backed users.** Even
  with Google as primary, the rescue user stays in htpasswd. Bigger
  user counts (thousands+) might justify a backing-store swap; not
  v1.5.

## Open follow-ups (small, not blocking ship)

- Decide on the customer-facing copy for the unauthorized landing
  page (`/oauth/unauthorized`). Constrained: no actions to offer
  (no request flow per the Known Issues section), so it's a plain
  page that says "you're signed in as &lt;email&gt;, but that account
  isn't authorized for any device. Contact your administrator if
  this is a mistake." Plus a Sign Out button so they can switch
  Google accounts and try again.
- Decide on the redirect-back-to-original-URL mechanism (state token
  parameter vs. separate `redirect_to` cookie set before the OAuth
  round-trip).
- Decide on display name in the admin header — email-only (locked
  default) vs. switching OAuth scope to include `profile` for the
  user's display name. Trivial swap if desired.
