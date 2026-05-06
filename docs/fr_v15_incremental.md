# v1.5 incremental rollout plan

## Context

The first v1.5 rollout attempt entangled three changes — code, build
chain, network topology — and shipped them together. When something
broke, the broken-thing surface was huge and trust collapsed. The
plan below was originally written as a "rebuild from 1.0" recovery;
in practice the operator rescued the build forward rather than
rolling back, but the discipline of the plan still applies: device
traffic must never be touched, and every phase ends at a verified
checkpoint that proves it.

This document has been updated to match the **actual deployed
topology** (which differs from the original FR draft) and to mark
phase status as of this writing.

## The non-negotiables (read these first; they bound every decision below)

1. **Device traffic is sacred.** The IoT path —
   `http://iot.stra2us.austindavid.com:8153/{q,kv,firmware}/...`
   with HMAC signatures — works in 1.0 and must continue to work
   unmodified through every phase. The hostname, the port, the
   protocol, and the route handlers all stay frozen for devices.

2. **Browser traffic moves to a SECOND path.** All v1.5 work happens
   on a separate hostname (`stra2us.austindavid.com`) that resolves
   to a separate ingress (Cloudflare's edge via tunnel). Browsers
   eventually only use the new path; devices never know it exists.

3. **Each phase ends at a checkpoint.** A checkpoint is a positive
   confirmation, not the absence of complaints — concretely: a real
   device's traffic visible in the activity log within the last
   60 seconds, AND admin login still works via the device-hostname
   rescue path. No phase ships without the checkpoint passing.

4. **No phase changes more than one variable at a time.** Code, deps,
   and topology each move independently. If a phase needs all three,
   it's actually three phases.

5. **Every phase has a written rollback.** Git revert + image
   re-tag, no improvisation.

## Two-path architecture (the end state — and the current state)

```
                                IoT devices
                                     │
                                     │ HTTP, port 8153, HMAC-signed
                                     ▼
iot.stra2us.austindavid.com ─── A ──► server public IP
                                     │
                                     │ docker port mapping
                                     ▼
                                 stra2us-iot container (FastAPI)
                                     ▲
                                     │ HTTP, internal docker network
                                     │
              ┌──────────────────────┘
              │
   stra2us-cloudflared container ──── outbound to CF edge
              ▲
              │ HTTPS, port 443
              │
stra2us.austindavid.com ─── CNAME ──► CF tunnel (CF edge IPs)
              ▲
              │
          Browsers
```

- **Devices** see only the upper half. DNS `iot.stra2us.austindavid.com`
  → A record to the server's public IP; HTTP on 8153 directly to the
  server. Identical to 1.0.
- **Browsers** see only the lower half. DNS `stra2us.austindavid.com`
  → CF edge; HTTPS on 443 to CF; CF tunnels to the same backend
  container over the docker internal network.
- **Single backend** serves both. The backend doesn't care which path
  a request arrived via — same routes, same handlers. The middleware
  just gates `/admin/`, `/api/admin/`, `/app/`, `/oauth/` (browser
  paths); `/q/`, `/kv/`, `/firmware/` (device paths) pass through
  untouched.

The key property: a network failure on the lower half (CF outage, DNS
issue, OAuth misconfig) cannot break the upper half. They share only
the backend container — which we don't touch in any phase that
doesn't need to. **This isolation property is also why the rescue
path through `iot.stra2us...:8153/admin/` worked when the first v1.5
attempt failed: the device hostname was unaffected by the broken
browser path.**

## Phase status as of this writing

| Phase | Description | Status |
|---|---|---|
| 0 | Stabilize the build (rescue forward, pin deps) | **Done** (rescue, not rollback) |
| 1 | Stand up the second hostname (CF tunnel) | **Done** |
| 2 | Add OAuth code, dormant behind feature flag | **Done** |
| 3 | Flag on, operator self-test of OAuth round-trip | **Done** |
| 4 | Hostname-aware middleware redirects browser → OAuth | **Done** |
| 4.5 | Build staging environment | TBD (gates Phase 5+) |
| 5 | Provisioning UI for granting access | TBD (requires staging) |
| 6 | Migrate operator off htpasswd; narrow to RESCUE_USERS | TBD |
| 7 | Optional cleanup of legacy browser access | TBD |

Build hygiene partially applied: `requirements.txt` is now pinned to
specific versions. The `requirements.lock.txt` workflow described
below has not been executed yet — left as a near-term TODO that
should land before the next dep bump, not as a Phase 4 blocker.

## Phase 0 — Functional smoke test (standing process, not a one-time event)

Phase 0 is **not** a rollback or a one-time stabilization step. It's
a standing functional test that runs after any rebuild — especially
any change to `requirements.txt` or the Docker image — to confirm
the deployment's invariants still hold before further phase work
proceeds.

The test is `tools/smoke_test.sh`. Bash + curl, no other deps.
Header of the script documents one-time setup of a smoke-test admin
user (htpasswd entry + wildcard ACL row in Redis). Once set up, run:

```sh
SMOKE_ADMIN_USER=smoke SMOKE_ADMIN_PASS='…' tools/smoke_test.sh
```

What it asserts:
- `/health` returns 200 on both hostnames.
- `/admin/` returns `401 WWW-Authenticate: Basic` on both hostnames
  (today's behavior; Phase 4 will change the browser-host case).
- `/oauth/google/login` returns 302 to `accounts.google.com/*` on
  both hostnames (proves the route is registered and the flag is on).
- `/oauth/google/callback` without params returns 400 (proves the
  `/oauth/` carve-out reaches the route handler without a session).
- `/oauth/bogus` returns 404 (proves the carve-out is route-bound,
  not a wildcard auth-skip).
- With creds: `/api/admin/logs?limit=1` returns a device heartbeat
  ≤60 seconds old (this is the FR's hard checkpoint — real device
  traffic). Failure messages distinguish bad creds (401), missing
  ACL row (403), too-narrow ACL (200 but empty), and stale heartbeat
  (200 but old).

When to run it:
- After any `docker compose build` or image rebuild.
- After editing `requirements.txt` or `requirements.lock.txt`.
- After any change to `main.py`, the auth middleware, or routing.
- As the closing checkpoint of every phase below — every phase ends
  by re-running the smoke test plus any phase-specific addition.

**Replaces the prior Phase 0 narrative.** The first v1.5 attempt
broke a uvicorn version drift; the recovery pinned `requirements.txt`
to specific versions. That history is preserved in the file's
header comment. Going forward, the discipline is "run the smoke
test, trust the receipts" — not "roll back if anything feels off."

## Phase 1 — Stand up the second hostname (DONE)

Goal: prove the two-path topology works without touching the backend
code.

What landed:
- CF tunnel created. Public hostname `stra2us.austindavid.com` →
  service `http://stra2us-iot:8153`.
- `cloudflared` service in `docker-compose.yaml`. Token in `.env`.
- DNS for `iot.stra2us.austindavid.com` is an A record to the server
  public IP (devices, unproxied).
- DNS for `stra2us.austindavid.com` is a CNAME to the CF tunnel
  (browsers, proxied).
- No split-horizon DNS — `iot.stra2us...` resolves to the public IP
  from anywhere (internal or external). The earlier split-horizon
  setup was unrolled during recovery.

**Phase 1 checkpoint (verified):**
- Real device heartbeat in activity log within 60 seconds.
- `curl http://iot.stra2us.austindavid.com:8153/health` → 200.
- `curl https://stra2us.austindavid.com/health` → 200.
- Admin htpasswd login works at both hostnames (same backend).

Rollback (if ever needed): stop and remove `cloudflared` from
compose; CF tunnel hostname can be left dangling (harmless).

## Phase 2 — OAuth code, dormant (DONE)

Goal: ship the OAuth route handlers behind a feature flag that
defaults OFF. Code lands but does nothing.

What landed:
- `backend/src/core/oauth.py` — config, `is_enabled()`, token
  exchange.
- `backend/src/api/routes_oauth.py` — `/oauth/google/login`,
  `/oauth/google/callback`. Both routes 503 when
  `is_enabled()` returns False.
- `main.py:231` registers the oauth router; `_path_needs_admin_auth`
  carves out `/oauth/`.
- `STRA2US_GOOGLE_OAUTH_ENABLED` env-var flag controls
  `is_enabled()`.
- Unit tests at `backend/tests/test_oauth.py`, fixtures in
  `conftest.py`.
- OAuth deps (`google-auth`, `requests`) added to
  `backend/requirements.txt` with pinned versions.

**Phase 2 checkpoint (verified):**
- Real device heartbeat in activity log within 60 seconds.
- htpasswd challenge unchanged on both hostnames.
- With flag off: `/oauth/google/login` → 503.
- Unit tests green: `cd backend && pytest tests/`.

## Phase 3 — Flag on, operator self-test (DONE)

Goal: turn the flag on, exercise the full Google round-trip end-to-
end, confirm a session cookie issues. Touch nothing else.

What landed:
- OAuth client registered in Google Console. Authorized redirect URI:
  `https://stra2us.austindavid.com/oauth/google/callback`.
- `STRA2US_GOOGLE_OAUTH_ENABLED=1`,
  `STRA2US_GOOGLE_CLIENT_ID`, `STRA2US_GOOGLE_CLIENT_SECRET`,
  `STRA2US_OAUTH_REDIRECT_URI` set in `.env` and propagated to the
  container via `docker-compose.yaml`.
- Operator's email provisioned in Redis with the correct ACL JSON
  shape (`{"permissions":[{"prefix":"*","access":"rw"}]}`).
- OAuth callback issues the same `admin_session` cookie the existing
  middleware already validates — no middleware change needed for the
  cookie to unlock `/admin/`.

**Phase 3 checkpoint (verified):**
- Real device heartbeat in activity log within 60 seconds.
- Operator successfully signs in via Google by manually navigating
  to `https://stra2us.austindavid.com/oauth/google/login`, completes
  the round-trip, lands at `/admin/` with a working session cookie.
- Operator can still sign in via htpasswd at the device hostname
  (rescue path).

Rollback (if ever needed): unset `STRA2US_GOOGLE_OAUTH_ENABLED` in
`.env`, `docker compose up -d`. OAuth routes go back to 503;
htpasswd unchanged.

## Phase 4 — Hostname-aware middleware (NEXT — not yet started)

Goal: when a browser hits the **browser hostname**
(`stra2us.austindavid.com`) without a session, redirect it to OAuth
instead of prompting for htpasswd. The device hostname continues to
serve htpasswd as the rescue path. Devices remain unaffected
(different hostname, different port, different protocol).

Steps:
1. Add a configured browser-host name (env var, default
   `stra2us.austindavid.com`) and a small helper:
   `_is_browser_host(request) -> bool` that compares against the
   `Host` header / `request.url.hostname`.
2. Modify `admin_auth_middleware` in `backend/src/main.py`. When the
   path needs admin auth and there's no valid cookie/Basic auth:
   - If `_is_browser_host(request)` and `oauth_config.is_enabled()`:
     302 to `/oauth/google/login?next=<original-url>`.
   - Else: today's 401 + `WWW-Authenticate: Basic realm="Admin Area"`
     (htpasswd challenge — preserves the device-hostname rescue).
3. Add a `next=` parameter round-trip in `routes_oauth.py` so
   post-login the user lands on the originally-requested URL
   (defaulting to `/admin/` when absent or not same-origin).
4. Device routes (`/q/`, `/kv/`, `/firmware/`) — middleware still
   skips them entirely via `_path_needs_admin_auth`. Unchanged.
5. Deploy. No dep change, no topology change.

**Phase 4 checkpoint:**
- Real device heartbeat in activity log within 60 seconds.
- Browser visiting `https://stra2us.austindavid.com/admin/` with no
  cookie → 302 to `/oauth/google/login`, Google round-trip, lands at
  `/admin/` (or the originally-requested URL).
- Browser visiting `http://iot.stra2us.austindavid.com:8153/admin/`
  with no cookie → htpasswd challenge (unchanged).
- Device routes on `iot.stra2us...:8153` work normally.

Rollback: revert the middleware change, redeploy. Feature flag
unaffected; OAuth routes still callable directly.

## Phase 4.5 — Build staging environment (gates Phase 5+)

Goal: a separate compose stack on a separate hostname with its own
Cloudflare tunnel, fed by the same image build pipeline, so that
Phase 5 onward can be validated end-to-end before touching production.

Phase 4 is small enough (a middleware diff plus a smoke-test
addition) that the existing smoke test plus a UI eyeball is adequate
verification. Phase 5 (provisioning UI) and Phase 6 (operator
migration off htpasswd) have larger surface area — admin forms,
error states, ACL JSON shapes, the rescue-path migration — and
warrant pre-prod validation.

Implementation tracker is in [`TODO.md`](../TODO.md) (top of the
Near-term list). Open questions captured there: where staging runs,
how staging gets device-heartbeat coverage for the smoke test's hard
checkpoint.

Out-clause: if Phase 4 verification turns up anything non-obvious
(smoke test catches a regression, UI eyeball reveals layout issues),
staging gets promoted from "before Phase 5" to non-negotiable —
nothing further ships through prod-only validation. Until then,
Phase 4 ships directly with smoke test + UI eyeball as the
checkpoint.

The smoke test already accepts `STRA2US_BROWSER_HOST` and
`STRA2US_DEVICE_HOST` env vars — once staging is up, the same script
runs against it unchanged. That's the minimum CI-shaped contract:
green smoke against staging is required before any phase ships to
prod.

## Phase 5 — Provisioning UI (TBD — requires staging from Phase 4.5)

Goal: replace the `redis-cli SET` step with a form. Eliminates the
ACL-shape footgun.

Scoped to the browser hostname. Device hostname unchanged. Details
TBD when we get here.

## Phase 6 — Migrate operator off htpasswd (TBD)

Goal: operator now signs in via Google by default. Htpasswd narrows
to a hardcoded `RESCUE_USERS` list (e.g. just `rescue`) used only on
the device hostname's `/admin/`, only when Google is unreachable.

Devices remain on HTTP/8153 with HMAC signing. They never see this.

## Phase 7 — Optional cleanup of the device-hostname admin path

Two viable options; the operator should pick before this phase
starts. (Both are deferrable indefinitely; today's setup is fine.)

**Option A — keep `iot.stra2us.austindavid.com:8153/admin/` reachable
with htpasswd narrowed to RESCUE_USERS.** Pro: simple rescue, the
exact path that saved the v1.5 recovery. Con: admin UI is publicly
reachable on HTTP/8153, protected only by basic auth.

**Option B — remove admin from the device hostname entirely.** The
device hostname serves only `/q/`, `/kv/`, `/firmware/`; everything
else returns 404 or 410. Rescue becomes "SSH to host, port-forward
or `docker exec` into the container". Pro: zero internet-exposed
admin. Con: rescue requires shell access (which is already needed
for any serious recovery, so the additional cost is small).

Devices still untouched in either option.

## Build hygiene (still applies going forward)

The collapse of trust came partly from `--build` silently pulling
new versions of every `>=` dep. To prevent recurrence:

1. **Lock file is the source of truth.** `requirements.lock.txt`
   captured via `pip freeze` against a known-good container. The
   loose `requirements.txt` exists as the human-readable list of
   *direct* deps; the Dockerfile installs from the lock.
   ```dockerfile
   # was:  RUN pip install --no-cache-dir -r requirements.txt
   # now:
   COPY requirements.lock.txt .
   RUN pip install --no-cache-dir -r requirements.lock.txt
   ```
   This step has not been executed yet (see Phase 0 status). Should
   land before the next dep bump.

2. **Tag the image at every phase.** Before
   `docker compose up -d --build`:
   ```sh
   docker tag stra2us-stra2us-iot:latest stra2us-stra2us-iot:phase-N-pre
   ```
   Rollback is then `docker tag stra2us-stra2us-iot:phase-N-pre stra2us-stra2us-iot:latest && docker compose up -d`.

3. **Adding a dep is a deliberate two-step:**
   - Add to `requirements.txt` with a specific version (`==`).
   - In a throwaway container: `pip install -r requirements.txt && pip freeze > requirements.lock.txt`.
   - Commit both files together.

4. **Don't over-rollback.** When a build problem traces to a
   dependency, fix forward with a tight pin near last-known-good.
   The dev window is short; deep rollbacks (months/years) create new
   compatibility problems and were what nearly sank the first v1.5
   attempt.

5. **Never `--no-cache` casually.** It rebuilds from scratch and
   will surface latent issues at the worst time.

## Things that must not happen during this rollout

- Changing DNS for `iot.stra2us.austindavid.com` (devices anchored).
- Removing the `0.0.0.0:8153->8153/tcp` port mapping.
- Adding middleware that touches `/q/`, `/kv/`, or `/firmware/`
  request paths in any way.
- Bumping deps without re-locking.
- Skipping a checkpoint to "save time."
- Running `--build` without first tagging the prior image.

## Open questions for the operator

- Phase 4: confirm the browser-host env var name and default. Proposed:
  `STRA2US_BROWSER_HOST=stra2us.austindavid.com`.
- Phase 4: how should the middleware behave if the request arrives
  on neither the configured browser host nor `iot.stra2us...`
  (e.g. an unexpected hostname)? Proposed: treat as device hostname
  (htpasswd challenge) by default — fail closed to the more
  conservative auth.
- Phase 7: Option A or Option B? Doesn't block Phase 4; useful to
  decide before Phase 6 lands.
- Build hygiene: when does the `requirements.lock.txt` workflow
  land? Suggested: a quiet window after Phase 4 verifies, before
  any further dep bumps.
