# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from api.routes_device import router as device_router
from api.routes_admin import router as admin_router
from api.routes_app import router as app_router
from api.routes_app_assets import router as app_assets_router
from api.routes_oauth import router as oauth_router
from middleware.csp import CSPMiddleware, router as csp_router
from core import oauth as oauth_config

app = FastAPI(title="IoT Telemetry Service")

import time
import base64
from urllib.parse import urlencode
from fastapi import Request, Response
from fastapi.responses import RedirectResponse
from core.redis_client import get_redis_client
from core.admin_auth import verify_password, generate_session_token, verify_session_token, is_rescue_on_default
from core.perf_log import DEFAULT_THRESHOLD_MS, write_perf_entry


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
    plus the customer-facing `/app/<app>/<device>/...` paths
    (fr_application_view.md). The bare `/app/` landing form, static
    assets under `/app/_static/`, and the `/api/app/lookup_device`
    endpoint stay public — a customer needs to be able to reach them
    BEFORE knowing their device URL or having a login.
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
    if path == "/app" or path == "/app/":
        return False  # bare landing form, public
    if path.startswith("/app/_static/"):
        return False  # public static assets — reuses the `_`-prefixed
                      # reserved-namespace convention from `_catalog/`
    if "/_assets/" in path and path.startswith("/app/"):
        return False  # per-app vendor asset bundle — public-by-design
                      # (the customer page references these in <img>
                      # tags). Same `_`-prefixed reserved-namespace
                      # convention as `/app/_static/`. See
                      # backend/src/api/routes_app_assets.py.
    if path.startswith("/api/app/"):
        return False  # public lookup endpoints
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
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as e:
        status = 500
        raise e
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
                log_status = f"Success ({status})" if 200 <= status < 300 else f"Error ({status})"

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

# Content Security Policy. Ships in Report-Only on every route per
# P0 of docs/fr_catalog_app_ui_plan.md — collect violations, no
# customer-facing behavior change. P3 adds `/app/<app>/<device>/`
# to `enforce_path_prefixes`; P5 flips the rest after the audit.
app.add_middleware(CSPMiddleware)
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
