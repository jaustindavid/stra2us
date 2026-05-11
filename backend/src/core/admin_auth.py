# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
import os
import hashlib
import hmac
import time
import base64
import json

HTPASSWD_FILE = os.environ.get(
    "STRA2US_HTPASSWD",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "admin.htpasswd"),
)

# Bootstrap default htpasswd file. Tracked in git; bootstrap-host.sh
# copies it into HTPASSWD_FILE on a fresh host so the rescue user
# exists from minute zero. Operator is expected to change the rescue
# password before exposing the device hostname; the startup check
# (`is_rescue_on_default`) flags when they haven't yet.
DEFAULT_HTPASSWD_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "admin.htpasswd.default",
)


def _find_user_line(filepath: str, user: str) -> str | None:
    """Return the raw `username:salt$hash` line for `user` in the file,
    or None if the user/file isn't present."""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{user}:"):
                    return line
    except OSError:
        return None
    return None


def is_rescue_on_default() -> bool:
    """True iff the live htpasswd's `rescue` entry is byte-for-byte
    identical to the entry shipped in admin.htpasswd.default. Once
    the operator runs `create_admin.py rescue <newpass>`, a fresh
    salt is generated and the entries diverge."""
    live = _find_user_line(HTPASSWD_FILE, "rescue")
    default = _find_user_line(DEFAULT_HTPASSWD_FILE, "rescue")
    return live is not None and default is not None and live == default

# HMAC signing key for admin session cookies. Resolution order:
#
#   1. ADMIN_SESSION_SECRET env var (operator override — useful
#      for local dev, tests, or if you ever need to force a
#      specific value).
#   2. /etc/stra2us/admin_session_secret — generated at image
#      build time by the Dockerfile, baked into the image. This
#      is the normal prod/staging path. All uvicorn workers in
#      the container read the same file → all agree on what
#      they've collectively signed. Rotates per image rebuild
#      (= per deploy), which forces admin re-login. OAuth makes
#      that cheap.
#   3. Per-process random fallback — only for local dev /
#      tests where neither (1) nor (2) is set. Safe under
#      single-worker; would break under multi-worker, but
#      multi-worker without the baked file or env var is an
#      operator error and we emit a warning to make it visible.
#
# Why not just env var: operator step is easy to forget; silent
# 75%-broken admin login under multi-worker is exactly the
# footgun we're trying to engineer away.
SESSION_SECRET_FILE = "/etc/stra2us/admin_session_secret"

def _load_session_secret() -> str:
    env_value = os.environ.get("ADMIN_SESSION_SECRET")
    if env_value:
        return env_value
    try:
        with open(SESSION_SECRET_FILE) as f:
            value = f.read().strip()
            if value:
                return value
    except OSError:
        pass
    # Fallback. Loud — if a multi-worker container ever hits
    # this, login will appear ~75% broken and the operator needs
    # the breadcrumb.
    import sys
    print(
        f"[admin_auth] WARNING: ADMIN_SESSION_SECRET env var unset "
        f"and {SESSION_SECRET_FILE} unreadable; using per-process "
        f"random secret. OK for single-worker dev; admin login "
        f"WILL break under multi-worker.",
        file=sys.stderr,
        flush=True,
    )
    return os.urandom(32).hex()

SESSION_SECRET = _load_session_secret()

def verify_password(username, password):
    if not os.path.exists(HTPASSWD_FILE):
        return False
        
    with open(HTPASSWD_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            
            uname, stored_hash = line.split(":", 1)
            if uname == username:
                if "$" not in stored_hash:
                    return False # Invalid format
                
                salt, expected_hash = stored_hash.split("$", 1)
                actual_hash = hashlib.sha256((salt + password).encode('utf-8')).hexdigest()
                return hmac.compare_digest(expected_hash, actual_hash)
                
    return False

def generate_session_token(username):
    # token format: base64(json({username, exp, signature}))
    exp = int(time.time()) + (24 * 3600) # 24 hour session duration
    payload = f"{username}:{exp}"
    signature = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    
    token_dict = {
        "u": username,
        "e": exp,
        "s": signature
    }
    return base64.b64encode(json.dumps(token_dict).encode()).decode()

def verify_session_token(token):
    """Validate a session cookie and return the username it authenticates, or
    None if invalid/expired. Older callers that treated this as a bool still
    work — None is falsy, a username string is truthy.
    """
    try:
        token_dict = json.loads(base64.b64decode(token).decode())
        username = token_dict.get("u")
        exp = token_dict.get("e")
        signature = token_dict.get("s")

        if not username or not exp or not signature:
            return None

        if int(time.time()) > int(exp):
            return None # Expired

        expected_payload = f"{username}:{exp}"
        expected_sig = hmac.new(SESSION_SECRET.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()

        if hmac.compare_digest(expected_sig, signature):
            return username
        return None
    except Exception:
        return None
