# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Google OAuth route handlers — login kickoff, callback, unauthorized
landing page.

Phase 1 of v1.5 (see docs/fr_v15_auth.md). Mounted behind a feature
flag (`STRA2US_GOOGLE_OAUTH_ENABLED`); when off, these routes don't
get registered, the existing basic-auth path stays sole.

The auth-middleware integration (i.e. "if no session cookie, redirect
to /oauth/google/login") lands in Phase 2 — Phase 1 ships the OAuth
flow as opt-in via direct navigation, so it can be exercised on
staging without disturbing the live auth path.
"""

import json
import logging
import os
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from core import oauth
from core.admin_auth import generate_session_token
from core.redis_client import get_redis_client
from api.dependencies import ADMIN_ACL_KEY_FMT


router = APIRouter()


# Dedicated logger for OAuth-flow telemetry. Stays narrow (just
# this module) so a future grep / log shipper can route on the
# `stra2us.oauth` channel name without picking up the FastAPI
# request log.
_oauth_log = logging.getLogger("stra2us.oauth")


# Cookie path is `/` so the session cookie applies to /admin/, /app/,
# /api/admin/, etc. uniformly. The temp cookies (state + redirect_to)
# also need to span at least the OAuth callback path; setting them on
# `/` is uniform and simpler than scoping per-cookie.
COOKIE_PATH = "/"

# Secure-cookie default. The `STRA2US_COOKIE_INSECURE` escape hatch
# exists for local dev where the page is served over plain HTTP and
# the browser won't accept Secure-flagged cookies. Production must
# leave this unset / false so cookies are HTTPS-only.
def _cookie_secure() -> bool:
    return os.environ.get("STRA2US_COOKIE_INSECURE", "").lower() not in ("1", "true", "yes")


@router.get("/oauth/google/login", include_in_schema=False)
async def oauth_login(
    request: Request,
    next_: str = Query("", alias="next"),
):
    """Kick off the OAuth round-trip. Generates a fresh state token,
    stashes it in a cookie, redirects to Google's authorize URL.

    The optional `next` query param carries the originally-requested
    URL when the middleware bounces an unauthenticated browser here
    (Phase 4). Stored in the `oauth_redirect_to` cookie; the callback
    reads it and redirects there post-login (or /admin/ if absent or
    malformed). Same-origin only — must start with a single `/`.

    Idempotent — calling repeatedly issues new state tokens and
    overwrites the cookie. This is fine; the only consequence is the
    older state token becomes unusable (which is the intent — single-
    use is a CSRF property)."""
    if not oauth.is_enabled():
        # Phase 1 is feature-flagged. If someone hits this URL when
        # OAuth isn't enabled, return a clear error rather than a
        # silent failure or a 500.
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not enabled on this server.",
        )

    state = oauth.generate_state_token()
    auth_url = oauth.build_authorize_url(state)

    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key=oauth.COOKIE_OAUTH_STATE,
        value=state,
        max_age=oauth.OAUTH_TEMP_COOKIE_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",  # required for the cookie to survive the
                         # cross-origin Google → callback redirect
        path=COOKIE_PATH,
    )
    # Stash `next` for the callback. Only honor same-origin paths:
    # must start with `/` AND not `//` (protocol-relative URLs like
    # `//evil.example/...` would let an attacker redirect off-origin
    # post-login, an OAuth-callback open-redirect footgun).
    if next_ and next_.startswith("/") and not next_.startswith("//"):
        response.set_cookie(
            key=oauth.COOKIE_OAUTH_REDIRECT_TO,
            value=next_,
            max_age=oauth.OAUTH_TEMP_COOKIE_TTL_SECONDS,
            httponly=True,
            secure=_cookie_secure(),
            samesite="lax",
            path=COOKIE_PATH,
        )
    return response


@router.get("/oauth/google/callback", include_in_schema=False)
async def oauth_callback(request: Request, code: str = "", state: str = ""):
    """Handle the redirect-back from Google. Validates the CSRF state,
    exchanges the code for an ID token, validates the token, looks up
    the email in admin_acls, issues a session cookie if authorized.

    Errors return a clear status + message rather than redirecting to
    a generic error page — easier to debug + the failure modes are
    distinct (CSRF mismatch vs. unauthorized vs. token-validation-
    failure all warrant different responses).
    """
    if not oauth.is_enabled():
        raise HTTPException(status_code=503, detail="Google OAuth is not enabled on this server.")

    # 1. CSRF defense — verify the state cookie matches the state param.
    state_cookie = request.cookies.get(oauth.COOKIE_OAUTH_STATE)
    if not oauth.verify_state_token(state, state_cookie):
        # Cookie missing OR mismatch. Either way: reject. Common
        # benign cause is the user opening the callback URL in a
        # different browser/private window than the one that started
        # the flow — friendly message rather than blunt 400.
        #
        # Instrumentation (filed in TODO.md as the "intermittent
        # 'Sign-in session expired or was forged'" item): log
        # WHICH case we're in so we can tell the three suspected
        # causes apart in real telemetry.
        #   - cookie missing  → multi-tab race or short-lived
        #     cookie expired before callback returned
        #   - cookie present  → mismatch; older tab racing newer
        #                       flow's overwrite, or samesite=lax
        #                       drop on cross-redirect
        # `state` itself is on the URL the browser already saw, so
        # logging its first 8 chars isn't a new disclosure; the
        # full-string compare lives inside `verify_state_token`.
        _oauth_log.warning(
            "csrf_mismatch",
            extra={
                "case": "cookie_missing" if state_cookie is None else "cookie_mismatch",
                "state_param_prefix": (state or "")[:8],
                "ua": request.headers.get("user-agent", "")[:120],
                "referer": request.headers.get("referer", "")[:200],
            },
        )
        return _error_response(
            400,
            "Sign-in session expired or was forged.",
            "Try signing in again. If you keep seeing this, make sure cookies are enabled.",
        )

    if not code:
        return _error_response(
            400,
            "Sign-in canceled or rejected.",
            "Google didn't return an authorization code. Try again.",
        )

    # 2. Exchange the code for an ID token.
    try:
        raw_id_token = oauth.exchange_code_for_id_token(code)
    except oauth.OAuthError as e:
        return _error_response(502, "Couldn't reach Google to complete sign-in.", str(e))

    # 3. Validate the ID token (signature, issuer, audience, expiry).
    try:
        claims = oauth.validate_id_token(raw_id_token)
    except oauth.OAuthError as e:
        return _error_response(401, "Sign-in token validation failed.", str(e))

    email = claims["email"]

    # 4. Authorize: do they have an admin_acls row?
    redis = get_redis_client()
    acl_raw = await redis.get(ADMIN_ACL_KEY_FMT.format(user=email))
    if not acl_raw:
        # Random Google sign-in or a known-but-not-yet-provisioned
        # customer. Render the friendly unauthorized page (does NOT
        # set the session cookie). Clear the temp cookies on the way
        # out — they've served their purpose.
        return _unauthorized_response(email)

    # 5. Authorized — issue session cookie, redirect to the original
    # destination (or /admin/ if none was preserved).
    redirect_to = request.cookies.get(oauth.COOKIE_OAUTH_REDIRECT_TO) or "/admin/"
    # Defensive: only allow same-origin redirects. Open redirect is a
    # known-OAuth-callback footgun.
    if not redirect_to.startswith("/"):
        redirect_to = "/admin/"

    session_token = generate_session_token(email)

    response = RedirectResponse(url=redirect_to, status_code=302)
    response.set_cookie(
        key="admin_session",
        value=session_token,
        max_age=7 * 24 * 60 * 60,    # 7 days, per FR
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path=COOKIE_PATH,
    )
    # Clear the temp cookies — they've done their job.
    response.delete_cookie(oauth.COOKIE_OAUTH_STATE, path=COOKIE_PATH)
    response.delete_cookie(oauth.COOKIE_OAUTH_REDIRECT_TO, path=COOKIE_PATH)
    return response


@router.get("/oauth/unauthorized", include_in_schema=False)
async def oauth_unauthorized(request: Request, email: str = ""):
    """Friendly landing for Google sign-ins that completed Google's
    side fine but don't have an `admin_acls:<email>` row.

    Displayed via the callback's redirect (with `?email=` param) when
    authorization fails. Reachable directly too (so the operator can
    preview the copy or customers can reload it after closing the tab).

    Per FR: no self-service request flow. Customer's only actions are
    to sign back in (different account) or contact their administrator
    out-of-band.
    """
    return _render_unauthorized_page(email)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _unauthorized_response(email: str) -> Response:
    """The full unauthorized response (page + temp-cookie cleanup).
    Factored so the callback can return it in one line."""
    response = _render_unauthorized_page(email)
    response.delete_cookie(oauth.COOKIE_OAUTH_STATE, path=COOKIE_PATH)
    response.delete_cookie(oauth.COOKIE_OAUTH_REDIRECT_TO, path=COOKIE_PATH)
    return response


def _render_unauthorized_page(email: str) -> HTMLResponse:
    safe_email = (email or "").replace("<", "&lt;").replace(">", "&gt;")
    body = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Not authorized</title>
    <style>
        body {{ font-family: system-ui, sans-serif; max-width: 480px;
               margin: 80px auto; padding: 0 20px; line-height: 1.5;
               color: #1a1a1c; }}
        h1 {{ font-size: 1.4rem; margin-bottom: 16px; }}
        .email {{ font-family: ui-monospace, monospace;
                 background: #f0f0f2; padding: 2px 6px; border-radius: 4px; }}
        .hint {{ color: #6b6b70; font-size: 0.95rem; }}
        a.btn {{ display: inline-block; margin-top: 20px; padding: 10px 16px;
                background: #2754c5; color: #fff; border-radius: 6px;
                text-decoration: none; font-size: 0.95rem; }}
    </style>
</head>
<body>
    <h1>You're signed in, but this account isn't authorized.</h1>
    {('<p>You signed in as <span class="email">' + safe_email + '</span>.</p>') if safe_email else ''}
    <p class="hint">
        That Google account doesn't have access to anything in
        stra2us. If this is a mistake — for example, you have
        multiple Google accounts and signed in with the wrong one —
        sign out and try again with a different account.
    </p>
    <p class="hint">
        Otherwise, contact your administrator. They'll need to add
        your Google email to grant access.
    </p>
    <a class="btn" href="/oauth/google/login">Sign in with a different account</a>
</body>
</html>
"""
    return HTMLResponse(body, status_code=403)


def _error_response(status: int, title: str, detail: str) -> HTMLResponse:
    """Generic 4xx page for OAuth flow failures (CSRF mismatch, token
    exchange failure, etc.). Distinct from the unauthorized page —
    this is "something went wrong with the sign-in itself," not "your
    account isn't authorized."""
    safe_detail = detail.replace("<", "&lt;").replace(">", "&gt;")
    body = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Sign-in error</title>
    <style>
        body {{ font-family: system-ui, sans-serif; max-width: 480px;
               margin: 80px auto; padding: 0 20px; line-height: 1.5;
               color: #1a1a1c; }}
        h1 {{ font-size: 1.4rem; margin-bottom: 16px; }}
        .hint {{ color: #6b6b70; font-size: 0.95rem; }}
        .detail {{ font-family: ui-monospace, monospace; font-size: 0.85rem;
                  background: #fdecea; color: #c53030; padding: 8px 12px;
                  border-radius: 4px; margin-top: 16px; }}
        a.btn {{ display: inline-block; margin-top: 20px; padding: 10px 16px;
                background: #2754c5; color: #fff; border-radius: 6px;
                text-decoration: none; font-size: 0.95rem; }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    <p class="hint">{safe_detail}</p>
    <a class="btn" href="/oauth/google/login">Try again</a>
</body>
</html>
"""
    return HTMLResponse(body, status_code=status)
