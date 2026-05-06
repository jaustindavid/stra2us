# FR: HMAC Response Signing

*Filed 2026-04-19 while scoping Phase 5 OTA IR for critterchron.*

## Problem

Stra2us authenticates device→server requests with HMAC-SHA256 over
`URI + Body + Timestamp` (see `core/security.py:20`, enforced in
`api/dependencies.py:37`). There is no corresponding mechanism in the
other direction: responses are plain msgpack/JSON bodies, and
`client/src/IoTClient.cpp` reads the status line, msgpack-decodes the
body, and returns. **A device cannot verify that a KV value or queue
message really came from the Stra2us server.**

For low-stakes tunables (heartbeep cadence, wobble rates) this is
tolerable — forging the value gets an attacker degraded timing. For
payloads that define device *behavior*, the gap becomes load-bearing.
Two concrete cases inside critterchron:

1. **Phase 5 OTA IR.** We are about to store compiled CritterEngine
   bytecode at `critterchron/scripts/<name>` as KV values. A forged
   response swaps in arbitrary agent logic on the device.
2. **ACL-sensitive queues.** Any future queue carrying "do thing X"
   messages has the same property.

Because the spec (`docs/spec.md:8`) explicitly *opts out of TLS* to
save MCU resources, the asymmetry cannot be papered over at the
transport layer. HMAC must run both directions.

## Proposal

Symmetric HMAC over response bodies, using the same per-client 32-byte
shared secret already in Redis.

**Wire format.** Server adds two headers to every `/kv/*` and `/q/*`
response:

```
X-Response-Timestamp: <unix-seconds>
X-Response-Signature: hex(hmac_sha256(secret, uri + body + ts))
```

Payload shape mirrors the request-signing layout — `URI + Body +
Timestamp`, same order, same concatenation — so `core/security.py` can
grow a single `calculate_signature(secret, payload, timestamp)` helper
used by both directions.

**Client verification.** `IoTClient::read_response_` grows a second
pass: after buffering status + headers + body, it re-HMACs `URI + body
+ X-Response-Timestamp` under the device's secret and compares against
`X-Response-Signature` using constant-time comparison. Mismatch → drop
the body and surface a failure to the caller (same path as a 5xx).

**Replay protection.** The request side uses a ±300s timestamp window
(`security.py:29`). The response side should too: if
`X-Response-Timestamp` is outside `[device_now - 300, device_now +
300]`, reject. Note this assumes the device clock is reasonably
synced — see "Clock bootstrap" under Related gaps below.

**Backwards compatibility.** Devices running pre-signing firmware
continue to accept unsigned responses. Once the server ships signing
and a device's firmware is updated, the device should *require* the
header and treat its absence as a failure — configurable via a
compile-time flag on the client for the rollout window, then hard
default after.

## Work estimate

- **Server**: ~30 lines across `core/security.py` (shared signer helper)
  and `routes_device.py` (inject headers after response body is
  finalized). Unit tests cover request + response symmetry.
- **Client**: ~50 lines in `IoTClient.cpp` — parse the two headers out
  of `read_response_`, call the existing `hmac_sha256` routine, wire the
  failure back up through `kv_fetch_` / `publish`. No new primitives.
- **Docs**: amend `docs/spec.md:8` and `docs/api.md` with the response
  contract.

## Related gaps surfaced during scoping

These came up while I was confirming the shape of the response-signing
problem. Out of scope for *this* FR but worth tracking, probably as
separate FRs. Each includes a file:line reference so the next person
doesn't have to re-grep.

- **P0 — Plaintext secrets in Redis.** `routes_admin.py:56` stores the
  raw 32-byte secret. Anyone with Redis access (or a Redis dump) can
  authenticate as every device. Mitigation: either encrypt at rest with
  an HSM/KMS-held key, or redesign around a device-held keypair + server
  public key (breaks the "minimize MCU crypto" constraint).
- **P1 — No in-band secret rotation.** `routes_admin.py:52` issues a
  secret at create time, visible in the response once. `routes_admin.py:
  76` revokes. There is no "rotate" flow that gives a device a grace
  period with two valid secrets, so rotation requires a firmware
  redeploy. Affects long-lived fielded devices.
- **P1 — Clock bootstrap catch-22.** Server enforces ±300s timestamp
  (`security.py:29`); device has no way to authenticate a time-sync
  response before its clock is valid. `/health` (`main.py:120`) is
  unsigned and could return a timestamp, but the device still can't
  verify the response. Resolving this cleanly likely depends on this FR
  (signed responses) landing first.
- **P2 — Admin password hashing.** `core/admin_auth.py:31` uses
  single-round SHA-256 with a non-random salt. Not the device-facing
  path, but an admin-UI compromise cascades into secret theft via
  `/api/admin/keys/backup` (`routes_admin.py:189`), so it indirectly
  affects device trust.
- **P2 — `/firmware/*` is unauthenticated** (`main.py:118`). Probably
  by-design (binaries are public artifacts) but worth an explicit
  decision in docs. If firmware ever embeds a per-device secret or
  other sensitive material, this becomes P0.

## What this FR is **not** proposing

- TLS. The spec deliberately avoids it; this FR preserves that choice.
- Per-message nonces. The ±300s timestamp window is sufficient for the
  current threat model; a nonce store adds server-side state we don't
  need yet.
- Admin-UI changes. Response signing is entirely device↔server.
