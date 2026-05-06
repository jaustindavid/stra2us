# Admin Auth Architecture

Scope: how admin identity is established (htpasswd + session cookie).
For what an authenticated admin is then allowed to do — the ACL
layer, per-route gating, superuser role, scoped-admin recipes — see
[acl_model.md](acl_model.md).

## The Core Challenge
The administrative dashboard web frontend sits at `/admin`. This exposes sensitive device secret generation buttons, visual key revocation protocols, and un-sanitized queue log traces. Since this project natively operates without bloated system dependencies like Postgres databases or extensive OAuth OIDC wrappers, it posed a unique security design challenge to lock it seamlessly away from public internet exposure.

## Final Implementation Strategy

We designed an entirely custom, zero-dependency middleware solution powered fundamentally by Python's `hashlib` and HTTP Protocol intrinsic features:

### 1. `admin.htpasswd` Fallback Engine
Instead of mapping users globally, we emulate classic Apache server configurations by establishing an `admin.htpasswd` flat file. 
- You manually push users and passwords into this via a command line `create_admin.py` generator.
- Passwords dynamically salt themselves and digest via `SHA-256`, remaining perfectly secure against filesystem penetration.

### 2. The HTTP Basic Native GUI
When an unrecognized visitor loads `/admin`, our FastAPI backend instantly intercepts via an `@app.middleware("http")` function evaluating routes matching `^/admin`. 
If they have no active credentials, it forcefully returns a `401 Unauthorized` HTTP code bundled inherently with the header `WWW-Authenticate: Basic realm="Admin Area"`.

This mechanism completely bypasses HTML login boxes and instructs modern browsers to deploy high-security native visual login modals instantly.

### 3. Session Cryptography Tokens (Cookies)
Upon successful verification of the basic password via `hmac.compare_digest`, our custom routing securely issues the client a Base64 encoded, HMAC-signed JSON token inside a `Set-Cookie: admin_session` boundary constraint.
If the backend decrypts this `admin_session` cookie signature validly upon subsequent queries, it immediately authorizes traffic, suppressing all recursive browser `Basic Auth` credential loops organically. 
