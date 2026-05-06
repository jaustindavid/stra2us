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

# In a production environment with multiple horizontally scaled workers, 
# ADMIN_SESSION_SECRET should be statically set as an environment variable 
# so all workers validate the same cookie. Otherwise, it generates a random 
# one per boot, which is fine for single-instance or session-bound routing.
SESSION_SECRET = os.environ.get("ADMIN_SESSION_SECRET", os.urandom(32).hex())

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
