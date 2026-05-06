"""Google OAuth helpers — token exchange, ID token validation,
state-token CSRF defense, redirect-back tracking.

Phase 1 of v1.5 (see docs/fr_v15_auth.md). Lives in core/ rather than
api/ so the validation + cookie helpers are unit-testable in isolation
from FastAPI routing.

Configuration is via env vars; the feature flag check is centralized in
`is_enabled()` so the rest of the codebase (route registration in
main.py, anywhere that branches on whether OAuth is live) doesn't have
to re-derive it.

Required env vars when enabled:
- STRA2US_GOOGLE_OAUTH_ENABLED      "1" / "true" turns Phase 1 on
- STRA2US_GOOGLE_CLIENT_ID          OAuth app's client_id (public)
- STRA2US_GOOGLE_CLIENT_SECRET      OAuth app's client_secret (private)
- STRA2US_OAUTH_REDIRECT_URI        e.g. https://prod.example.com/oauth/google/callback

Cookie names (set by the route handlers, parsed here):
- oauth_state              CSRF defense — opaque random token
- oauth_redirect_to        URL to bounce back to after successful auth
- admin_session            existing session cookie (issued on success)
"""

import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import requests
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# These cookie TTLs are short on purpose — they only need to survive the
# round-trip to Google and back. Longer windows widen the CSRF attack
# surface without buying anything.
OAUTH_TEMP_COOKIE_TTL_SECONDS = 600  # 10 min

# Cookie names. Pinned here so the route handlers and tests reference
# the same constants — avoids the "typo'd cookie name silently breaks
# the round-trip" failure mode.
COOKIE_OAUTH_STATE = "oauth_state"
COOKIE_OAUTH_REDIRECT_TO = "oauth_redirect_to"

# Where Google's OAuth endpoints live. Hardcoded; these don't change.
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# What we ask for from Google. Pinned to the minimum needed: `openid`
# is the OIDC base scope (gives us the ID token), `email` adds the
# user's verified email address (which is what we look up in
# `admin_acls:` to authorize). Adding `profile` would give us the
# user's display name + avatar URL but adds PII surface; defer per FR.
# NOT requesting `offline_access` — we never need to talk to Google
# again after sign-in (cookie + refresh-on-activity covers our 7-day
# session model), so no refresh tokens.
GOOGLE_SCOPE = "openid email"

# Google's well-known issuer string for ID tokens — `google-auth`'s
# `verify_oauth2_token` accepts either form, but pinning here means
# the test mocks have a single source of truth.
GOOGLE_ISSUER = "https://accounts.google.com"


def is_enabled() -> bool:
    """True if Phase 1 OAuth is feature-flag-on. Default off so the
    code can ship dormant; operator opts in by setting the env var."""
    return os.environ.get("STRA2US_GOOGLE_OAUTH_ENABLED", "").lower() in ("1", "true", "yes")


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"OAuth is enabled but {name} is not set. See "
            f"docs/fr_v15_auth.md > 'Operational checklist for Phase 1'."
        )
    return val


def client_id() -> str:
    return _required_env("STRA2US_GOOGLE_CLIENT_ID")


def client_secret() -> str:
    return _required_env("STRA2US_GOOGLE_CLIENT_SECRET")


def redirect_uri() -> str:
    return _required_env("STRA2US_OAUTH_REDIRECT_URI")


# ---------------------------------------------------------------------------
# State-token (CSRF defense)
# ---------------------------------------------------------------------------
#
# Standard OAuth `state` param protocol: we generate a random token,
# stash it in a cookie, include it in the authorize URL, then verify
# the callback's `state` query param matches the cookie. Mismatch →
# reject (callback was forged or replayed).
#
# Stored in its OWN cookie (not as part of the OAuth `state` param's
# encoded payload). The state param is for CSRF defense; conflating it
# with redirect-back tracking has size + encoding pitfalls, and the
# two responsibilities have nothing to do with each other.

def generate_state_token() -> str:
    """Opaque random token for OAuth state. URL-safe, 32 bytes of entropy."""
    return secrets.token_urlsafe(32)


def verify_state_token(received: Optional[str], stored: Optional[str]) -> bool:
    """Constant-time compare of the callback's state param against the
    cookie value. Both must be present; either missing → reject."""
    if not received or not stored:
        return False
    return secrets.compare_digest(received, stored)


# ---------------------------------------------------------------------------
# Authorize-URL construction (login redirect)
# ---------------------------------------------------------------------------

def build_authorize_url(state: str) -> str:
    """Construct the URL we 302 the browser to so Google can show its
    consent screen. State token is the CSRF defense; the
    authentication-back-to-us happens at our redirect_uri."""
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "state": state,
        # Force the consent screen + account picker on every sign-in so
        # the user can choose which Google account; otherwise Google
        # silently uses whatever's signed in.
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token exchange + ID-token validation
# ---------------------------------------------------------------------------

class OAuthError(Exception):
    """Anything that goes wrong in the token exchange or ID-token
    validation. Surfaced as 4xx by the route handler."""


def exchange_code_for_id_token(code: str) -> str:
    """POST to Google's token endpoint; trade an authorization code for
    a set of tokens. Returns the raw ID token (a JWT) for downstream
    validation. We discard the access token + refresh token — we don't
    need to call any Google APIs on the user's behalf, just identify them.

    Raises `OAuthError` on any HTTP / parsing failure.
    """
    try:
        r = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id(),
                "client_secret": client_secret(),
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
    except requests.RequestException as e:
        raise OAuthError(f"Token endpoint network error: {e}") from e

    if r.status_code != 200:
        raise OAuthError(f"Token exchange returned HTTP {r.status_code}: {r.text[:200]}")

    try:
        payload = r.json()
    except ValueError as e:
        raise OAuthError(f"Token endpoint returned non-JSON: {e}") from e

    id_tok = payload.get("id_token")
    if not id_tok:
        raise OAuthError("Token endpoint response missing id_token")
    return id_tok


def validate_id_token(raw_token: str) -> dict:
    """Validate signature against Google's JWKS, check issuer +
    audience + expiration. Returns the verified claims dict on success;
    raises `OAuthError` on any validation failure.

    google-auth handles the heavy lifting (JWKS caching, signature alg
    checks, clock skew). We just need to pass our client_id as the
    expected audience.
    """
    try:
        claims = id_token.verify_oauth2_token(
            raw_token,
            google_requests.Request(),
            audience=client_id(),
        )
    except ValueError as e:
        # google-auth raises ValueError for any validation failure
        # (bad signature, wrong issuer, expired, audience mismatch).
        raise OAuthError(f"ID token validation failed: {e}") from e

    # Belt-and-suspenders issuer check (verify_oauth2_token does this
    # too, but the failure message is clearer if we explicitly assert).
    iss = claims.get("iss")
    if iss not in ("accounts.google.com", "https://accounts.google.com"):
        raise OAuthError(f"ID token issuer is not Google: {iss!r}")

    if not claims.get("email"):
        raise OAuthError("ID token has no email claim")

    if not claims.get("email_verified"):
        # Unverified emails are spoofable by the user. Reject.
        raise OAuthError("ID token email is not verified")

    return claims
