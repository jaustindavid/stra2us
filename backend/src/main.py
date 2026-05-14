# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
import sys
import os
import logging
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from api.routes_device import router as device_router
from api.routes_admin import router as admin_router
from api.routes_app import router as app_router
from api.routes_app_assets import router as app_assets_router
from api.routes_app_form import router as app_form_router
from api.routes_app_theme import router as app_theme_router
from api.routes_oauth import router as oauth_router
from middleware.csp import CSPMiddleware, router as csp_router
from core import oauth as oauth_config

app = FastAPI(title="IoT Telemetry Service")

import time
import base64
from urllib.parse import urlencode
from fastapi import Request, Response
from fastapi.responses import RedirectResponse
from starlette.requests import ClientDisconnect
from core.redis_client import get_redis_client
from core.admin_auth import verify_password, generate_session_token, verify_session_token, is_rescue_on_default
from core.perf_log import DEFAULT_THRESHOLD_MS, write_perf_entry


# Structured-error logger. Mirrors the `stra2us.oauth` logger added
# in v1.6.1 for OAuth CSRF instrumentation. Used by the activity-log
# middleware to emit a context-tagged traceback whenever a request
# raises through to the middleware boundary — `/kv/` and `/q/` should
# never 500 (HMAC failure → 401, valid miss → 200 with not_found body),
# so a 500 here is by definition a bug; making the log line easy to
# find with structured request context (path + client_id) is what
# makes "rare intermittent 500" tractable to debug.
_err_log = logging.getLogger("stra2us.errors")


# Configured browser-facing hostname (the Cloudflare-tunneled path).
# A request whose Host matches this hostname AND whose session is
# missing gets a 302 to /oauth/google/login. Anything else (the
# device hostname, an unknown Host header, a raw-IP probe) falls
# through to the htpasswd challenge — fail-closed default that
# preserves the iot.stra2us... rescue path. See
# fr_v15_incremental.md Phase 4.
BROWSER_HOST = os.environ.get("STRA2US_BROWSER_HOST", "stra2us.austindavid.com")


def _is_browser_host(request: Request) -> bool:
    # Behind the Cloudflare tunnel, request.url.hostname is the
    # internal docker service name (e.g. "stra2us-iot") because
    # cloudflared rewrites the Host header to the configured
    # upstream service URL. The original public hostname survives
    # in X-Forwarded-Host. Prefer that when present; fall back to
    # request.url.hostname for direct (non-tunneled) access. Take
    # only the first comma-separated value — the header can carry
    # a list when multiple proxies are in path.
    fwd = request.headers.get("x-forwarded-host", "")
    host = fwd.split(",")[0].strip() if fwd else request.url.hostname
    return host == BROWSER_HOST


def _path_needs_admin_auth(path: str) -> bool:
    """True for paths that should be gated by the admin htpasswd / cookie
    auth flow. Includes the canonical `/admin*` and `/api/admin*` paths
    plus the customer-facing `/app/...` paths (fr_application_view.md,
    including v1.7.1 Sprint 3's tightening of the bare `/app/` landing
    form). Public exceptions: OAuth login flow, logout, static assets,
    per-app theme + asset bundles, and CSP-violation reports —
    anything a browser or device needs to reach without a session.
    """
    if path.startswith("/oauth/"):
        return False  # OAuth login/callback/unauthorized must be reachable
                      # without a session — that's their whole purpose.
                      # See fr_v15_auth.md.
    if path == "/admin/logout":
        return False  # Logout must always work, even with a corrupted
                      # session cookie — otherwise you can't escape a
                      # broken state without clearing cookies manually.
    if path.startswith("/admin") or path.startswith("/api/admin"):
        return True
    if path.startswith("/app/_static/"):
        return False  # public static assets — reuses the `_`-prefixed
                      # reserved-namespace convention from `_catalog/`
    if "/_assets/" in path and path.startswith("/app/"):
        return False  # per-app vendor asset bundle — public-by-design
                      # (the customer page references these in <img>
                      # tags). Same `_`-prefixed reserved-namespace
                      # convention as `/app/_static/`. See
                      # backend/src/api/routes_app_assets.py.
    if path.startswith("/app/") and path.endswith("/_theme.css"):
        return False  # per-app theme stylesheet — public-by-design.
                      # Body is hex colors + allowlisted font names;
                      # nothing sensitive. Customer page references
                      # via <link rel="stylesheet">. See
                      # backend/src/api/routes_app_theme.py.
    # v1.7.1 Sprint 3: gate `/app/` landing form AND
    # `/api/app/lookup_device` behind admin auth. Pre-v1.7.1 both
    # were intentionally public — the customer needed to resolve a
    # device name → app *before* OAuth could gate the per-device
    # page. That made `/api/app/lookup_device` enumerable: an
    # unauthenticated attacker could probe device names and learn
    # which exist + which app each belongs to. OAuth + the admin
    # allowlist now handles enumeration-prevention without a CAPTCHA
    # dependency. UX cost: one OAuth roundtrip on first visit per
    # session; the session cookie covers subsequent visits.
    if path == "/app" or path == "/app/":
        return True   # landing form — was public pre-v1.7.1
    if path == "/api/app/lookup_device":
        return True   # device-name resolver — was public pre-v1.7.1
    if path.startswith("/api/app/"):
        return False  # other public app endpoints (none today;
                      # forward-compat for hypothetical future
                      # public app-facing endpoints)
    if path == "/api/_csp_report":
        return False  # browsers POST CSP violations same-origin without
                      # a session — the underscore-prefixed reserved
                      # namespace marks this as server metadata, not
                      # an admin-action endpoint. See
                      # backend/src/middleware/csp.py.
    if path.startswith("/app/"):
        return True   # /app/<app>/<device>/... — auth required
    return False


@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    if _path_needs_admin_auth(path):
        # Check cookie first
        cookie = request.cookies.get("admin_session")
        if cookie:
            cookie_user = verify_session_token(cookie)
            if cookie_user:
                request.state.admin_user = cookie_user
                return await call_next(request)

        # Check Basic Auth
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Basic "):
            try:
                encoded_creds = auth_header.split(" ")[1]
                decoded_creds = base64.b64decode(encoded_creds).decode("utf-8")
                username, password = decoded_creds.split(":", 1)

                if verify_password(username, password):
                    # Valid — hand the username to downstream deps for ACL checks.
                    request.state.admin_user = username
                    response = await call_next(request)
                    token = generate_session_token(username)
                    response.set_cookie(key="admin_session", value=token, httponly=True)
                    return response
            except Exception:
                pass # Fall through to 401

        # Not authenticated. Two paths:
        #   - Browser host + OAuth enabled → 302 to /oauth/google/login
        #     with the originally-requested URL preserved as ?next=.
        #   - Anything else → htpasswd challenge (today's behavior;
        #     preserves the iot.stra2us...:8153 rescue path and
        #     fails closed for unexpected Host headers).
        if _is_browser_host(request) and oauth_config.is_enabled():
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(
                url=f"/oauth/google/login?{urlencode({'next': target})}",
                status_code=302,
            )
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Admin Area"'})

    return await call_next(request)

@app.middleware("http")
async def activity_log_middleware(request: Request, call_next):
    # Track the exception class across try/except → finally so the
    # activity-log entry can include it inline (e.g. "Error (500)
    # [TimeoutError]"). Pre-v1.6.6 the activity log only said
    # "Error (500)", so an operator hunting an intermittent 500 had
    # to correlate stdout tracebacks by timestamp. Tagging by class
    # makes a single XREVRANGE pass enough to see the distribution
    # of exception types.
    exc_name = None
    try:
        response = await call_next(request)
        status = response.status_code
    except ClientDisconnect:
        # Client closed the TCP connection mid-request — typically
        # during the body read inside `verify_device_request`'s
        # dependency resolution, before the handler frame even
        # appears in the stack. The handler never started, no
        # Redis writes, no signing, no side effects — this is a
        # network reality, not a server bug.
        #
        # Status 499 is nginx's "Client Closed Request" — non-
        # standard but the established convention for this case.
        # No `_err_log.exception(...)` call: tracebacks for
        # disconnects are noise (one per flaky-link retry),
        # whereas the activity-log entry below preserves the
        # signal for traffic-pattern visibility.
        #
        # Re-raise anyway so Starlette's outer handling continues
        # cleanly; the response we'd send is moot since the
        # client is already gone.
        status = 499
        exc_name = "ClientDisconnect"
        raise
    except Exception as e:
        status = 500
        exc_name = type(e).__name__
        # Structured-context error log. `logger.exception` automatically
        # captures the traceback. The format-args carry the request
        # context FastAPI's default exception handler doesn't provide
        # (path, method, client_id) — surrounds the bare traceback with
        # everything needed to localize "which request? which device?".
        # Contract: /kv/ and /q/ should never 500, so any line here is
        # a bug to investigate, not noise.
        _err_log.exception(
            "Unhandled exception in %s %s (client=%s)",
            request.method, request.url.path,
            request.headers.get("X-Client-ID", "unknown"),
        )
        raise
    finally:
        path = request.url.path
        # Log device data APIs (queues + KV).
        if path.startswith("/q/") or path.startswith("/kv/"):
            if (
                request.method == "GET"
                and path.startswith("/kv/")
                and 200 <= status < 300
                and hasattr(request.state, "kv_hit")
            ):
                # KV reads return 200 even on miss (handler ships
                # `{"status":"not_found"}` body — devices fall back to
                # defaults without 404 handling). The handler stashes
                # `kv_hit` on request.state to let us emit the right
                # log status here. See routes_device.py:read_kv.
                client_id = request.headers.get("X-Client-ID", "unknown")
                log_status = "Hit (200)" if request.state.kv_hit else "Miss (200)"
            else:
                client_id = request.headers.get("X-Client-ID", "unknown")
                if 200 <= status < 300:
                    log_status = f"Success ({status})"
                elif status == 499:
                    # Distinct from `Error (...)` — these aren't server
                    # errors. Devices on flaky links retry; recording
                    # the disconnects without the alarmist "Error"
                    # framing keeps the activity log honest about
                    # what's actually a server-side problem.
                    log_status = f"Client disconnect ({status})"
                elif exc_name:
                    log_status = f"Error ({status}) [{exc_name}]"
                else:
                    log_status = f"Error ({status})"

            log_entry = {
                "timestamp": int(time.time()),
                "client_id": client_id,
                "action":    f"{request.method} {path}",
                "status":    log_status,
            }

            redis = get_redis_client()
            await redis.xadd("system:activity_log", {
                "timestamp": str(log_entry["timestamp"]),
                "client_id": log_entry["client_id"],
                "action":    log_entry["action"],
                "status":    log_entry["status"],
            }, maxlen=150000, approximate=True)
            # `MAXLEN ~ 150000` already bounds the stream — the previous
            # per-request age-based xtrim added a redundant round-trip on
            # every device call. If a strict 24h window matters later, do
            # it from a periodic job rather than the request hot path.

    return response


@app.middleware("http")
async def admin_cache_control_middleware(request: Request, call_next):
    """Force `Cache-Control: no-cache` on `/admin/*` static
    responses (P5 #1d followup). Browsers still keep the bytes
    cached but always revalidate with the server before using
    them — so a deploy that ships new admin JS/HTML/CSS reaches
    operators on their next page load instead of after a manual
    Shift+Reload.

    Same shape of fix as P4's `Cache-Control: no-store` on the
    customer page, but `no-cache` rather than `no-store` because
    the admin shell is static (304-able) — letting the browser
    keep the bytes between requests is the right perf trade. The
    cost is one round-trip per file per session (the 304); for
    typical admin file sizes that's microseconds.

    Vendored assets (`/admin/_vendor/...`) get the same treatment
    even though they're effectively immutable — the round-trip
    cost is negligible and a uniform rule beats two-rule
    bookkeeping. If `/admin/_vendor/inter/inter-latin.woff2`
    revalidation ever shows up in perf logs, switch the vendor
    path to `max-age=31536000, immutable`.
    """
    response = await call_next(request)
    if request.url.path.startswith("/admin/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def perf_log_middleware(request: Request, call_next):
    """Times every dynamic request; appends to system:perf_log when
    total_ms >= STRA2US_PERF_LOG_THRESHOLD_MS. Defined last so it wraps
    auth and activity-log work — total_ms reflects user-perceived latency.
    Static assets, the health/root probes, and the perf-log read endpoint
    are skipped (noise / self-reference)."""
    path = request.url.path
    skip = (
        path.startswith("/admin/")
        or path == "/api/admin/perf_log"
        or path in ("/", "/health")
    )
    if skip:
        return await call_next(request)

    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        total_ms = (time.perf_counter() - start) * 1000.0
        if total_ms >= DEFAULT_THRESHOLD_MS:
            phases = getattr(request.state, "perf_phases", None)
            client_id = (
                getattr(request.state, "admin_user", None)
                or request.headers.get("X-Client-ID")
                or (request.client.host if request.client else "unknown")
            )
            try:
                await write_perf_entry(
                    method=request.method,
                    path=path,
                    total_ms=total_ms,
                    status_code=status_code,
                    client_id=client_id,
                    phases=phases,
                )
            except Exception as e:
                # Never let perf logging break a request.
                print(f"[PERF_LOG] write failed: {e}", flush=True)


# Allow CORS for development convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Content Security Policy — **enforcing across all routes** as of
# P5 #1d. The history:
#
#   * P0 introduced the middleware in Report-Only with the FR's
#     full strict policy.
#   * P5 first flip: customer-facing `/app/*` to enforcing,
#     admin/api stay Report-Only (different cleanup readiness).
#   * P5 #1d (this state): admin/api also enforcing, after the
#     #1a-c admin cleanup landed:
#       - js-yaml + Inter font self-hosted (`backend/src/static/_vendor/`)
#       - all inline `style=` lifted to CSS classes (~30 across
#         index.html + app.js template literals)
#       - all inline `onclick=` / `onchange=` lifted to a single
#         delegated dispatcher reading `data-action` /
#         `data-change-action` (~50 callsites)
#
# `enforce_default=True` flips every route. The CF Insights
# allowance (script-src + connect-src + the
# `static.cloudflareinsights.com` host) is handled inside
# `build_policy` and applies uniformly. The
# `report_only_path_prefixes` knob is empty — nothing is
# deliberately staying behind. If a future admin tweak
# re-introduces a violation, the user sees a console error
# immediately rather than a slow telemetry trickle; the
# `Reporting-Endpoints` header still wires the report-uri so
# violations land in the `stra2us.csp` log either way.
#
# Rollback path: pass `enforce_default=False, enforce_path_prefixes=["/app/"]`
# to revert to the prior partial-flip shape. CSP is config, not
# data — flips are a deploy, not a migration.
app.add_middleware(
    CSPMiddleware,
    enforce_default=True,
)
app.include_router(csp_router, tags=["csp"])

# Admin API
app.include_router(admin_router, prefix="/api/admin", tags=["admin"])

# Device API
app.include_router(device_router, tags=["device"])

# Logout endpoint. Registered BEFORE the /admin static mount so this
# route wins path resolution. Behavior splits by hostname so the
# response is appropriate to the auth scheme in play:
#
#   - Browser host (OAuth path): respond 200 with a plain HTML
#     "Signed out" page. No Basic-Auth dialog. User clicks "Sign in
#     again" to re-enter the OAuth flow.
#
#   - Device host (htpasswd path): respond 401 with WWW-Authenticate
#     realm="logged-out" (different from the live realm "Admin Area").
#     The realm change is what flushes Chrome's cached Basic Auth
#     credentials — without it, "log out" is impossible without
#     quitting the browser. The user's browser may briefly flash a
#     Basic Auth dialog; closing it leaves them logged out cleanly.
#
# Both paths clear admin_session, oauth_state, oauth_redirect_to
# cookies so the server-side view of the session is also gone.
from fastapi.responses import HTMLResponse

_LOGOUT_HTML = """<!doctype html>
<html><head><title>Signed out</title>
<style>
  body { background: #0d1117; color: #c9d1d9;
         font-family: system-ui, -apple-system, sans-serif;
         display: flex; align-items: center; justify-content: center;
         height: 100vh; margin: 0; }
  .card { text-align: center; }
  h2 { font-weight: normal; color: #fff; margin: 0 0 16px; }
  a { color: #00f0ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style></head>
<body><div class="card">
  <h2>Signed out.</h2>
  <p><a href="/admin/">Sign in again</a></p>
</div></body></html>
"""


@app.get("/admin/logout", include_in_schema=False)
async def admin_logout(request: Request):
    if _is_browser_host(request):
        response = HTMLResponse(content=_LOGOUT_HTML, status_code=200)
    else:
        response = Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="logged-out"'},
            content="Signed out.\n\nTo sign in again, navigate to /admin/.\n",
            media_type="text/plain",
        )
    # Path="/" matches how the OAuth callback (and the Basic Auth
    # path) set these. Without an explicit path, delete_cookie
    # wouldn't match the OAuth-issued cookies.
    response.delete_cookie("admin_session", path="/")
    response.delete_cookie("oauth_state", path="/")
    response.delete_cookie("oauth_redirect_to", path="/")
    return response


# Mount Static UI
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/admin", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True), name="static")

# Customer-facing application view (see docs/fr_application_view.md).
# Static assets live at `/app/_static/` (underscore-prefixed reserved
# namespace, public per the auth middleware). Mount BEFORE the router
# so the mount claims that path prefix first; the dynamic routes
# (`/app/`, `/app/{app}/{device}`) catch everything else under /app/.
APP_STATIC_DIR = os.path.join(BASE_DIR, "static", "app")
app.mount("/app/_static", StaticFiles(directory=APP_STATIC_DIR), name="app-static")
# Per-app catalog asset serving (P1 of fr_catalog_app_ui_plan.md).
# Mount BEFORE app_router so the more specific
# `/app/{app}/_assets/{filename}` route resolves before
# `/app/{app}/{device}` (which would otherwise capture
# `_assets` as a device name).
app.include_router(app_assets_router, tags=["app-assets"])

# Per-app theme stylesheet (P2 of fr_catalog_app_ui_plan.md).
# Same mount-order rationale: `_theme.css` is a reserved name that
# would otherwise be captured as a device id by /app/{app}/{device}.
app.include_router(app_theme_router, tags=["app-theme"])

# Customer-facing form-submit handler (P3 of fr_catalog_app_ui_plan.md).
# The POST handler matches the GET path's auth + ACL gating, so no
# auth-middleware exception is needed — the existing
# `_path_needs_admin_auth("/app/<app>/<device>")` rule covers both
# methods. Mount BEFORE app_router so its POST route doesn't
# get shadowed by the GET-only handlers there.
app.include_router(app_form_router, tags=["app-form"])

# No prefix on the router — routes_app declares its own (`/app/...` and
# `/api/app/...`) so the route handlers' paths read naturally and
# the auth middleware's path-matching logic stays in one place.
app.include_router(app_router, tags=["app"])

# Google OAuth routes (Phase 1 of v1.5 — see fr_v15_auth.md).
# Feature-flagged: routes ALWAYS register so they can return a clear
# 503 when called with the flag off (better than 404 + confusion). The
# routes themselves check `oauth.is_enabled()` and 503 internally.
# When the operator opts in via STRA2US_GOOGLE_OAUTH_ENABLED=1 + sets
# the client_id/secret/redirect_uri env vars, the flow becomes live.
# Phase 2 will plumb the redirect-from-no-cookie path into the auth
# middleware; Phase 1 ships only direct navigation to /oauth/google/login
# so the flow can be exercised on staging without disturbing the live
# auth path.
app.include_router(oauth_router, tags=["oauth"])

# Note: the legacy `/firmware/` static-file route was removed in
# 2026-05-06; firmware is now stored as KV blobs and fetched via
# the regular `/kv/` device path. See git log for the removal commit.

# Soft warning at startup if the rescue user is still on the
# bootstrap-default password. Loud version (admin-UI banner) lives
# in routes_admin / app.js — see /api/admin/security_warnings.
if is_rescue_on_default():
    _border = "=" * 70
    print(_border, flush=True)
    print("WARNING: 'rescue' user is on the bootstrap-default password.", flush=True)
    print("Change it before exposing the device hostname to anything", flush=True)
    print("sensitive:", flush=True)
    print("    cd backend && python3 create_admin.py rescue '<new-password>'", flush=True)
    print(_border, flush=True)


@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/")
def read_root():
    return {"status": "IoT Telemetry Service is running. Access /admin for Management UI."}
