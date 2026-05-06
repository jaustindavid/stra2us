# FR: Per-Key Encrypted KV Values

*Filed 2026-05-02 while scoping confidentiality for critterchron's
procyon rescue WiFi flow.*

## Status (as of 2026-05-03)

**Shipped — safe to deploy:**
- Server: HMAC-keystream cipher in [`core/security.py`](../backend/src/core/security.py),
  GET/POST/DELETE handlers in [`api/routes_device.py`](../backend/src/api/routes_device.py)
  honor a `kv:{key}:enc` sidecar, admin POST gained `encrypted: bool`
  on `KVPayload`, `peek_kv` surfaces the flag.
- CLI: `stra2us set ... --encrypted` and `stra2us put ... --encrypted`
  on the writer side; `client.get()` transparently decrypts ext type
  0x21 on the reader side. See [`tools/stra2us_cli/client.py`](../tools/stra2us_cli/client.py).
  CLI catalog validator (`tools/stra2us_cli/catalog.py`) accepts the
  consumer-side `encrypted: bool` field on Var entries.
- Admin UI: 🔒 badge on encrypted rows in the dashboard list, an
  Encrypted checkbox on the KV editor modal (pre-fills from current
  state to avoid silent demote-on-save), and an "Encrypted: yes/no"
  line in the peek modal.
- C++ device decrypt path (Particle Photon/Photon 2/Argon + ESP32-C3).
  `kv_fetch_str_` recognizes the msgpack ext family (fixext1..16,
  ext8/16/32) with type byte 0x21; `kvenc_xor_` mirrors the server's
  cipher byte-for-byte; `read_response_` exposes the verified
  X-Response-Timestamp via an out-param so the decrypt can use it as
  the keystream nonce. Sources:
  [`hal/particle/src/Stra2usClient.cpp`](https://github.com/austin/critterchron/blob/main/hal/particle/src/Stra2usClient.cpp),
  [`hal/esp32/src/Stra2usClient.cpp`](https://github.com/austin/critterchron/blob/main/hal/esp32/src/Stra2usClient.cpp).
  Validated end-to-end on rachel (Photon 2) on 2026-05-02:
  encrypted `wifi_password` → ext 0x21 wire fetch → device decrypt →
  WiFi.setCredentials → device joined target network.
- Reference C++ SDK (`client/src/IoTClient.{h,cpp}`): `kvencXor`
  primitive, `lastResponseTimestamp()` accessor, and a
  `decryptKVResponseIfEncrypted` convenience helper that detects the
  full ext family (fixext1..16, ext8/16/32) with type 0x21, decrypts
  in place, and updates the caller's length. **Illustrative reference
  code for future device ports** — mirrors critterchron's HAL
  line-for-line but is not itself compile-tested or production-
  deployed. The canonical production-validated implementation lives
  in critterchron's HAL (linked above). A 4th-platform port should
  start here, run the cross-language test vectors below as a
  smoke-check on its toolchain, then verify against a real device.
- Test coverage: 7 unit tests pin the cipher wire format including a
  cross-impl agreement check (server's `kvenc_xor` byte-for-byte equal
  to CLI's `_kvenc_xor`); 5 live-server integration tests cover
  encrypted roundtrip, multi-block keystream, per-key isolation,
  demote-on-bare-set, and the ext-0x21 wire-form contract; 5 admin-API
  live tests cover the `KVPayload.encrypted` flag (set/clear/demote/
  Pydantic-default/delete-clears-sidecar). All 53 pass against a real
  uvicorn instance.

**Cross-language test vectors:**

Generated from the Python reference implementation
([`tools/stra2us_cli/client.py:_kvenc_xor`](../tools/stra2us_cli/client.py))
on 2026-05-03. Any conforming port must reproduce these byte-for-byte
before going live. The 33-byte case is the keystream-counter-rolls
guard; the 63-byte case is the realistic WPA2 max-length input.

| name | secret_hex (32B) | nonce | plaintext (hex) | ciphertext (hex) |
|---|---|---|---|---|
| 1B  | `00…00` | `0`          | `00`                                                                                                                                 | `06` |
| 32B | `00…00` | `1`          | `00…` (×32)                                                                                                                          | `b9a9ca963328b9c3b2740905b1c9e48b4d86b6a339de2ee9ef5e5a087b7a7bde` |
| 33B (counter rolls) | `00…00` | `1` | `00…` (×33)                                                                                                                  | `b9a9ca963328b9c3b2740905b1c9e48b4d86b6a339de2ee9ef5e5a087b7a7bde8a` |
| typical wifi pw | `ab…ab` | `1714608000` (`0x6632d780`) | `68756e746572322d776966692d70617373776f72642d68657265`                                                       | `93cc04aa952f30619fd6d78bd971388ea02cd95df78ec966bfec` |
| 63B WPA2 max | `cd…cd` | `3735928559` (`0xdeadbeef`) | `78` × 63                                                                                                       | `9c4fa502aab925e99e55cf5632623efdf9d4b9bdfb3236205b10c4f0c4427e26f21fa6d0b2934b6af09a515e1fdbbc743d8f4a59223516374f344198c2796d` |

(`00…00` = 32 bytes of `0x00`; `ab…ab` = 32 bytes of `0xab`; etc.
The 33-byte ciphertext extends the 32-byte one by exactly one byte
because that's the first byte of the counter=1 block — a useful sanity
check that any port computes both `counter=0` and `counter=1` blocks
correctly.)

**Operational gotchas worth knowing:**
- *Rotation.* Encrypted KVs are keyed off the per-client shared secret.
  Rotating a client's secret renders that client's prior encrypted
  values unreadable on the wire — re-write any encrypted KVs for that
  client after rotation. (Same threat-model as request/response
  signing, called out below under "Forward secrecy.")
- *Two-step write.* Setting an encrypted record is `SET kv:{key}` then
  `SET kv:{key}:enc` — non-atomic. A server crash between the two ops
  can leave the flag out of sync with the value. Bounded blast radius;
  a re-set fixes it. Worth a Redis pipeline if anyone wants the
  atomicity guarantee.

## Problem

Stra2us authenticates traffic in both directions (HMAC-SHA256 over
`URI + Body + Timestamp` for requests, response signatures via the
mechanism in `fr_response_signing.md`). Authenticity and integrity
are covered: a passive observer can't forge a value or swap a queue
message under either direction.

**Confidentiality is not.** Response bodies are plaintext msgpack;
the spec explicitly opts out of TLS (`docs/spec.md:8`) to save MCU
resources. For low-stakes tunables (heartbeep cadence, render
budgets, rescue-mode visual thresholds) this is tolerable — there is
nothing to keep secret about a rendering parameter.

The gap becomes meaningful when the KV value is **operator-supplied
secret material** rather than a tunable knob. The motivating example
is critterchron's `wifi_password` key, used by the procyon rescue
flow (see `critterchron/PROCYON.md`):

1. Operator at install site sets `wifi_password` in Stra2us so the
   target device can self-install its primary network credentials
   the first time it joins procyon.
2. Device on procyon (an internet-tethered phone hotspot) fetches
   the value via plaintext HTTP.
3. Anyone with passive sniff capability on the procyon network
   during that fetch reads the wifi_password in cleartext.

The threat is bounded — physical proximity to the procyon hotspot is
required, and procyon-mode use is rare and operator-supervised — but
"don't operate the rescue flow on networks where you wouldn't trust a
passive sniffer" is operational discipline that's easy to forget.

A symmetrically-applied solution (encrypt every KV value) is
unattractive: the dozens of non-sensitive knobs in a typical app
catalog (e.g., critterchron's `heartbeep`, `max_brightness`,
`brightness_schedule`) materially benefit from `curl | less`
debuggability of their wire form, which we leaned on heavily during
testing of OTA IR, firmware OTA, and procyon. We want a per-key
opt-in.

## Proposal

Per-record `encrypted` flag in Stra2us storage. When set, the GET
handler encrypts the value before responding using a stream cipher
keyed by the requesting device's existing shared secret. Wire-format
marker on the response so the client knows when to decrypt.

### Wire format

Two changes to the response body for encrypted values:

1. **Marker.** Wrap the encrypted payload in a msgpack `ext` type
   (e.g., type code `0x21`) so a generic msgpack reader can detect
   "this is an encrypted critterchron value" before attempting to
   parse it as a string. The wrapped payload itself is the raw
   ciphertext bytes; the original msgpack-shape (str8/16/32, bin8/
   16/32) is **not** preserved across the wire — the client knows
   the type from the catalog or context.

   Alternative considered: a fixed prefix byte (e.g., `0xFF`) before
   the existing msgpack str/bin payload. Rejected because it
   collides with valid msgpack negative-fixint encoding (`0xFF =
   -1`), making generic decoders ambiguous.

2. **Nonce.** Reuse the existing `X-Response-Timestamp` header
   (already present per the response-signing FR) as the per-call
   nonce. No new header. The server SHOULD ensure timestamp
   monotonicity within a (device, record) pair so the client can
   detect replays — but this is already a property of the response-
   signing scheme.

### Cipher

HMAC-SHA256 stream cipher (NOT bare XOR with the secret):

```
keystream = HMAC-SHA256(secret, label || nonce || counter)
            // counter increments per 32-byte block until keystream
            // length >= plaintext length
ciphertext = plaintext XOR keystream
```

- `secret` is the per-client 32-byte shared secret already used for
  HMAC request/response signing. **No new key material.**
- `label` is a fixed ASCII string (e.g., `"stra2us-kvenc-v1"`) to
  domain-separate this keystream from any other HMAC use of the
  same secret.
- `nonce` is the response timestamp (uint32 BE, 4 bytes).
- `counter` is a uint8 starting at 0; each subsequent HMAC call
  increments it. For values longer than 32 bytes (e.g., a 63-byte
  WPA2 password), two HMAC calls produce 64 bytes of keystream.

**Why HMAC-keystream, not bare XOR with the secret:** bare XOR is
trivially broken by known-plaintext attacks. WiFi passwords have
known structure (ASCII, length conventions); recovering even one
ciphertext means recovering the secret directly via XOR cancellation.
The HMAC layer's one-way property means a known-plaintext attack
recovers only the keystream — which is itself an HMAC output and
cannot be inverted to the secret.

**Why not AES-128-CTR:** mbedTLS-based AES is a real dependency
addition on both Particle and ESP32. HMAC-SHA256 is already in the
codebase (used for both request signing and response signing), so
HMAC-keystream is "free" in code surface. The marginal security
improvement of AES over HMAC-keystream against the actual threat
model (passive sniffer, no MITM because of response signing) is
small enough that the dep cost wins.

### Storage schema

Each Redis-backed KV record gains an `encrypted: bool` field
(default false). The flag is set by the writer at `set` time and
honored by the GET handler.

### CLI

`stra2us set` gains an `--encrypted` flag:

```
stra2us set <device> wifi_password <pw> --encrypted
```

When set, the CLI marks the record's encrypted flag in the write.
Idempotent: setting `--encrypted` on a record that's already
encrypted leaves it encrypted; setting *without* `--encrypted` on a
previously-encrypted record demotes it to plaintext (which is a
sensible "I changed my mind" semantic). A future `stra2us encrypt
<key>` / `stra2us decrypt <key>` could lift this to a value-
preserving in-place flip if needed.

`stra2us list` should indicate encrypted records visibly (e.g., a
🔒 prefix or `[encrypted]` annotation in the value column) so an
operator scanning the catalog state knows what's confidential.

`stra2us get` of an encrypted record from a CLI session (which
authenticates as the same client_id as the device) returns the
decrypted plaintext, since the CLI holds the same secret. This
preserves operational debuggability: an operator with the right
credentials can still see what's there.

### Catalog hint (consumer-side, e.g., critterchron)

Apps that consume the catalog can declare which keys are expected to
be encrypted via an `encrypted: true` field in the YAML. Stra2us
itself does not enforce this — the per-record flag is what governs
wire behavior — but consumer drift tests can verify "every catalog
entry marked `encrypted: true` is actually stored that way" and
"keys whose names match `password|secret|key` are marked
encrypted." See `critterchron/STRA2US_CATALOG_FR.md` for the
consumer-side spec.

## Work estimate

Server (this repo):

- Schema migration: add `encrypted` field to KV record model
- GET handler: detect flag, compute keystream, XOR, wrap in ext
  type
- Tests for both encrypted-roundtrip and the plaintext fallthrough

CLI:

- `--encrypted` flag on `set`
- `list` formatting to surface encrypted records

Total estimate: ~150 LOC + tests. One sitting if Stra2us internals
are straightforward; two if the Redis schema migration needs care.

## Related gaps surfaced during scoping

- **Catalog-server linkage.** Today the per-app catalog YAML lives
  in the consuming repo (e.g., `critterchron/critterchron.s2s.yaml`)
  and Stra2us has no knowledge of it. An "encrypted" field in the
  catalog is advisory. The per-record flag in Stra2us is what
  actually controls wire behavior. This is the same architectural
  layering as `ops_only` (catalog hint, not server-enforced).
- **Forgot-to-mark-sensitive risk.** Mitigated on the consumer side
  by drift-test name-pattern lints (`password|secret|key` →
  must-be-encrypted). Not Stra2us's problem to enforce, but worth
  flagging here so we don't accidentally bake a bad default into the
  protocol.
- **Encrypted queues.** Out of scope here — this FR is KV-only. If
  encrypted queue messages become useful later, the same wire
  marker + cipher could extend; the GET handler's logic generalizes
  cleanly. File separately when needed.

## What this FR is *not* proposing

- **TLS.** Still opted out per the original spec. This FR adds
  per-key confidentiality without a transport-layer dep.
- **Bulk encryption of every KV value.** Per-key opt-in, by design.
  Operational ergonomics (curl/less debuggability of non-sensitive
  values) is genuinely useful and worth preserving.
- **Authenticated encryption.** The response-signing FR already
  provides authenticity over the entire response body, including
  encrypted-value responses. We don't need a separate AEAD construct.
- **Asymmetric crypto.** Per-client shared secrets are already the
  authn primitive; this FR uses the same. No new key distribution
  story.
- **Forward secrecy.** A device's secret being compromised should be
  treated as a full client compromise — encrypted historical values
  retained on the wire (e.g., in operator captures) become readable.
  This is the same threat model as the existing request/response
  signing.
