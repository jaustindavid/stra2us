# Stra2us Client Implementor's Guide

*Drafted 2026-05-03 from the accumulated experience of three
independent client implementations (Python CLI, Particle C++, ESP32-C3
C++). If you're writing a fourth, start here.*

This document is the practical "how to build a client" companion to
[`spec.md`](spec.md) (high-level service description),
[`fr_response_signing.md`](fr_response_signing.md) (HMAC over response
bodies), and [`fr_encrypted_values.md`](fr_encrypted_values.md)
(per-key encrypted KV values). Read those for design rationale; this
document focuses on what a correct client *has* to do, plus the sharp
edges we hit during implementation.

## Quick reference

| Concern                  | What you need to do                                                    | Reference impl                                                   |
|--------------------------|------------------------------------------------------------------------|------------------------------------------------------------------|
| Authn (request signing)  | HMAC-SHA256 over `URI \|\| body \|\| ts_ascii_decimal`, hex-encode    | `tools/stra2us_cli/client.py::_sign_payload`                     |
| Authn (response verify)  | Streaming HMAC over body, ±300s drift, fail-closed on missing headers  | `hal/particle/src/Stra2usClient.cpp::read_response_`             |
| Value shapes             | Accept str + bin families for the same logical string KV value        | `kv_fetch_str_` in either C++ client                             |
| "Key not found"          | nil (`0xc0`) and fixmap (`0x80-0x8f`) → silent miss, NOT an error      | same                                                             |
| Encrypted values         | ext type `0x21` → HMAC-keystream decrypt before returning to caller    | `kvenc_xor_` in either C++ client                                |
| Connection: close        | Honor it — close your end after body read; do not reuse the socket     | server-will-close branch in `read_response_`                     |
| Large fetches (~1MB)     | Stream via chunk callback, HMAC over chunks not buffered body         | `kv_fetch_stream_` in `hal/esp32/src/Stra2usClient.cpp`          |
| Threading                | All network I/O off the render path; ≥8KB tel-thread stack            | `telemetry_task` in either critterchron .ino/.cpp                |

## Wire basics

### Transport

HTTP/1.1 over TCP. **No TLS** — the spec explicitly opts out to keep
MCUs cheap. Confidentiality and authenticity are layered above HTTP via
HMAC-SHA256 in both directions.

### Endpoints

| Method | Path                | Purpose                                                  |
|--------|---------------------|----------------------------------------------------------|
| `GET`  | `/kv/{key}`         | Read a KV value. `key` may contain `/` for namespacing.  |
| `POST` | `/kv/{key}`         | Write a KV value (msgpack body). `?ttl=N` optional.      |
| `POST` | `/q/{topic}`        | Publish a queue message.                                 |
| `GET`  | `/q/{topic}`        | Consume one queue message (FIFO, removed on read).       |

Bodies are **msgpack** — no JSON, no form-encoded. Even an int "5"
should land on the wire as the single byte `0x05` (msgpack positive
fixint) or one of the int variants.

### Request headers (required)

| Header          | Value                                                          |
|-----------------|----------------------------------------------------------------|
| `X-Client-ID`   | The client's identifier registered with Stra2us                |
| `X-Timestamp`   | Unix seconds, ASCII decimal (no fractional part)               |
| `X-Signature`   | HMAC-SHA256 over `URI \|\| body \|\| ts_ascii_decimal`, lowercase hex |
| `Content-Type`  | `application/msgpack` for POSTs (omit for GET)                |
| `Connection`    | `keep-alive` is fine; server may respond with `close` regardless |

The signing payload is the literal byte sequence `URI` (path only, no
host or query) followed by the request body bytes (empty for GET)
followed by the timestamp as ASCII decimal. **Do not** include the
`?ttl=...` query string in URI for the signature.

### Response headers (always present on 2xx)

| Header                  | Value                                                         |
|-------------------------|---------------------------------------------------------------|
| `X-Response-Timestamp`  | Server-side unix seconds at the moment the response was built |
| `X-Response-Signature`  | HMAC-SHA256 over `URI \|\| response_body \|\| ts`, lowercase hex |
| `Connection`            | Often `close`. Honor it — see "Connection lifecycle" below.   |

A 2xx response **without** signing headers MUST be treated as a
failure. Failing closed is the only safe behavior — an unsigned 2xx
could be an MITM or a misconfigured server, and there's no way to
distinguish from authentic.

## Request signing

```
signature = HMAC-SHA256(secret_32_bytes, URI || body || ts_ascii_decimal)
```

Implementation notes:

- `secret` is the per-client 32-byte raw key (clients store it as
  64-char hex; decode to 32 bytes before HMAC).
- `body` is empty (zero-length) for GET requests. Don't pass `nil`,
  pass an empty byte slice.
- `ts` is ASCII decimal (e.g. `"1777767309"`), not packed binary, and
  not the same string as the JSON-style with quotes.
- Signature is hex-encoded lowercase, no `0x` prefix, no separators.

Reference: `_sign_payload` in
[`tools/stra2us_cli/client.py`](../tools/stra2us_cli/client.py).

## Response verification

Same HMAC primitive applied to the response. Two non-obvious
constraints:

### Stream the HMAC, don't buffer the body

For small responses (KV reads, queue messages) buffering is fine. For
large responses (the OTA-IR or firmware-OTA blobs in critterchron's
case, ~1MB) you cannot afford to hold the entire body in RAM on a
microcontroller. The pattern:

1. Initialize HMAC context with `secret`
2. Feed the URI bytes
3. As body bytes arrive from TCP, feed them to HMAC and to the caller's
   chunk consumer (or copy buffer if small)
4. After the last body byte, feed the timestamp ASCII bytes
5. Finalize HMAC, hex-encode, constant-time compare against
   `X-Response-Signature`

Reference: `read_response_` in
[`hal/particle/src/Stra2usClient.cpp`](https://github.com/austin/critterchron/blob/main/hal/particle/src/Stra2usClient.cpp).

### Drift window (±300s)

Reject a response whose `X-Response-Timestamp` differs from the
client's local `Time.now()` by more than 300 seconds. Mirrors the
server's check on request timestamps. Implementation: only check if the
local clock is set (e.g., `Time.isValid()` or equivalent) — pre-NTP
boot has `Time.now() == 0` and would always fail the drift check
otherwise. Skip the check during pre-clock-valid bootstrapping.

### Fail closed on missing headers

A 2xx response with `X-Response-Timestamp` or `X-Response-Signature`
absent or empty MUST be rejected. There is no fallback to "trust the
HTTP layer" — there is no HTTP layer security here.

### Constant-time hex compare

Use a hex-equality routine that reads all 64 chars of both signatures
regardless of where they first diverge. Off-the-shelf `strcmp`
short-circuits on the first mismatched byte and leaks the prefix to a
timing attacker. Critterchron's `hex_equal_` is the model — XOR each
byte pair into an accumulator, return whether the accumulator is zero
*after* the loop completes.

## msgpack value shapes

Stra2us serializes string-typed KV values with whichever msgpack
encoding the server happens to pick. A correct client accepts the
**str family AND the bin family** for the same logical "string"
because both have the same length-prefix-then-bytes layout:

| Marker      | Family   | Length encoding             | Payload offset |
|-------------|----------|-----------------------------|----------------|
| `0xa0-0xbf` | fixstr   | `marker & 0x1f` (0–31)      | +1             |
| `0xd9`      | str8     | next byte (0–255)           | +2             |
| `0xda`      | str16    | next 2 bytes BE             | +3             |
| `0xdb`      | str32    | next 4 bytes BE             | +5             |
| `0xc4`      | bin8     | next byte                   | +2             |
| `0xc5`      | bin16    | next 2 bytes BE             | +3             |
| `0xc6`      | bin32    | next 4 bytes BE             | +5             |

The IR-OTA blob is bin16 (0xc5) in critterchron's deployment;
`brightness_schedule` is fixstr or str8. Don't bake either choice into
your parser.

### Numeric values

For int/float KV values (e.g. tunable knobs), accept the standard
msgpack families: positive/negative fixint, int8/16/32/64, uint8/16/32/64,
float32 (`0xca`), float64 (`0xcb`). See `kv_fetch_` in either C++
client for the full dispatch.

### Absent-key signals (silent miss)

When a key isn't set, the server may reply with a 2xx response whose
body is **nil** (`0xc0`) or a small **fixmap** (`0x80-0x8f`, error
envelope dict like `{"status": "not_found"}`). Both must be treated as
"key not found" without recording an error. Returning the error to a
caller is correct; logging it as a fetch failure is wrong — operators
will set keys lazily and absence is normal.

This bit us early on: the original Particle/ESP32 clients logged
`kvs msgpack hdr=0x81` on every absent-key fetch, drowning the
heartbeat error channel in noise from `wifi_password` keys that
weren't set yet.

### Encrypted values (ext family)

The msgpack ext types `0xd4-0xd8` (fixext1/2/4/8/16) and `0xc7-0xc9`
(ext8/16/32) signal an encrypted KV value when the type byte is
`0x21`. Layout per encoding:

| Marker      | Length     | Type byte position | Payload offset |
|-------------|------------|--------------------|----------------|
| `0xd4`-`0xd8`| 1/2/4/8/16 (fixed)| +1                 | +2             |
| `0xc7`      | next byte  | +2                 | +3             |
| `0xc8`      | next 2 BE  | +3                 | +4             |
| `0xc9`      | next 4 BE  | +5                 | +6             |

If the type byte is anything other than `0x21`, fail closed. Future
ext types added by Stra2us will need explicit client support.

## Encryption

Per-key encrypted KV values use an HMAC-keystream stream cipher, fully
specified in [`fr_encrypted_values.md`](fr_encrypted_values.md). The
operative bits a client implementor needs:

```
keystream = HMAC-SHA256(secret, "stra2us-kvenc-v1" || nonce_BE || counter)
            // counter increments per 32-byte block
plaintext = ciphertext XOR keystream  // symmetric
```

- **secret**: same 32-byte per-client shared secret used for
  request/response signing
- **label**: literal ASCII bytes `"stra2us-kvenc-v1"`, 16 bytes, no NUL
- **nonce**: the response's `X-Response-Timestamp` value parsed as a
  uint32, encoded big-endian (4 bytes)
- **counter**: 1-byte uint8 starting at 0; produces 32 bytes of
  keystream per HMAC call. Concatenate blocks until you have enough
  bytes. Cap at 255 (= 8 KiB plaintext ceiling) for safety.

Reference: `kvenc_xor_` in
[`hal/particle/src/Stra2usClient.cpp`](https://github.com/austin/critterchron/blob/main/hal/particle/src/Stra2usClient.cpp).

The decrypt happens **inside the KV-fetch helper**, transparently to
callers. From the application's perspective, fetching an encrypted
string KV returns the same `(value, length)` as fetching a plaintext
one. The `encrypted` bit is a server/transport detail, not a caller
concern.

## Connection lifecycle

Stra2us (uvicorn / ASGI) responds with `Connection: close` after every
request. **Honor it** — close your end after reading the body, before
the next request. Two reasons:

1. **The server's already done.** Reusing the socket on the next
   request means writing to a half-closed peer; eventually you get a
   timeout or a write error.
2. **ESP32-specific bug**: arduino-esp32's `WiFiClient::connected()`
   doesn't notice the server's FIN until it's fully propagated locally.
   Critterchron observed multi-minute hangs where the client thought
   the connection was alive, sent a request into a black hole, and
   waited for the response timeout. The fix was to close eagerly after
   any response that carried `Connection: close`.

Pattern: parse the response's `Connection:` header (case-insensitive
"close"), and if present, call `close()` (or `tcp.stop()` /
`socket.close()`) at the end of the response-handling function.
Independent of whether the response status was 2xx or an error.

## Streaming fetches

For large KV values (~1MB+ — critterchron uses this for IR-OTA blobs
and firmware OTAs), buffering the entire body is not viable on a
microcontroller. Pattern:

```
client.kv_fetch_stream_(key, chunk_callback, userdata, &out_size)
```

- `chunk_callback(userdata, bytes, len) -> bool` is invoked for each
  body fragment as it arrives off the wire. The msgpack length-prefix
  header is consumed internally by the fetch helper; the callback only
  ever sees raw payload bytes.
- Returning `false` from the callback aborts the fetch. The fetch
  helper still drains the socket up to `Connection: close` boundary
  before returning, so subsequent requests don't see leftover bytes.
- HMAC verification runs over the streamed bytes (not a buffered copy)
  so the cipher cost scales with payload size, not memory.

Reference: `kv_fetch_stream_` in
[`hal/esp32/src/Stra2usClient.cpp`](https://github.com/austin/critterchron/blob/main/hal/esp32/src/Stra2usClient.cpp).
The encrypted-value path does **not** currently extend to streaming
fetches — encrypted streams aren't a use case yet (firmware OTA blobs
aren't sensitive in critterchron's threat model). If you need it,
extend the chunk-callback pattern to also XOR each chunk against the
keystream.

## Threading & resource model

In the critterchron HALs, network I/O lives on a dedicated FreeRTOS
task (ESP32) or DeviceOS thread (Particle), distinct from the render
loop. Reasons:

- Network reads can block for hundreds of ms; the render loop wants
  20ms cycles
- Cloud heartbeat publishes lock the socket for a multi-second round
  trip
- An IR-OTA fetch can run for tens of seconds streaming a large blob

Stack sizing observation (from
[`debug_ota_hardfault_stack.md`](https://github.com/austin/critterchron/blob/main/.claude/projects/-Users-austin-src-claude-sandbox-critterchron/memory/debug_ota_hardfault_stack.md)):
the tel thread needs **≥8KB** when IR-OTA is enabled. Hard fault during
a blob fetch with the default 4KB Particle thread stack — the HMAC
context plus the IR buffer plus the TCP scratch overflowed silently.
On ESP32, FreeRTOS gives more headroom; default of 8KB is adequate.

Latency tracking: an `LatencyScope` RAII helper around `publish` and
`kv_fetch_*` calls produces per-op samples that flow into the
heartbeat as `latency=<min,mean,max>`. Useful for spotting a stuck
network without running an external tracer.

## Error surfacing

Recommended pattern: a small ring buffer of error categories and
detail strings, drained by the heartbeat publish.

```c
struct ErrEntry {
    ErrCat cat;          // Net, OtaFetch, Boot, Other, ...
    char   msg[64];      // free-form detail
    uint32_t seq;        // monotonic for mark-sent dedup
};
```

The heartbeat builder reads the oldest unsent entry, includes it as
`err=<cat>:<msg>`, and marks it sent only on a successful publish — so
a transient publish failure leaves the entry queued for the next
heartbeat rather than being lost.

Critterchron's categories (drop in for a starting set):

- `Net` — WiFi/cloud reconnect kicks, transient connectivity events
- `OtaFetch` — KV read failures (bad msgpack, HMAC mismatch, payload too big)
- `Boot` — engine startup, rescue-hold trigger
- `Other` — catch-all

Don't over-categorize early. The signal you actually care about is
"did some specific behavior happen at heartbeat time," and a 64-char
detail string is usually the part that matters.

## Catalog interplay

### `ops_only`

A consumer-side hint that means "the device doesn't read this key via
the cache-based `get_int`/`get_float` path; it has a dedicated
accessor." Drift tests that enforce "every catalog entry has a
matching get_* call site" exempt `ops_only: true` entries. Stra2us
itself doesn't act on this flag.

### `encrypted`

**Two layers**. The catalog hint (`encrypted: true` in the YAML) is a
consumer-side declaration — it tells the consumer's drift test "this
key better also be stored encrypted on the server." The server-side
per-record `encrypted` bit is what actually controls wire behavior.
They must agree, but neither enforces the other directly. The pattern
that keeps them in sync: a CI rule that fails if any catalog
`encrypted: true` key isn't actually marked encrypted in the running
Stra2us.

Critterchron's drift test also includes a **name-pattern lint**: any
catalog key whose name matches `/password|secret|key/i` MUST have
`encrypted: true`. Catches the "added a future sensitive knob and
forgot to mark it" risk.

### Drift testing

Catalog ↔ code agreement is best enforced as a CI test, not at
runtime. Critterchron's
[`test_s2s_catalog.py`](https://github.com/austin/critterchron/blob/main/test_s2s_catalog.py)
verifies:

1. Every `get_int(key, default)` / `get_float(key, default)` call site
   in C++ has a matching catalog entry
2. Every catalog entry not marked `ops_only: true` has at least one
   call site
3. Catalog `default:` literals match the C++ default-expression
   resolution (modulo per-device / per-platform overrides)
4. (Encrypted lint) any sensitive-named key is marked encrypted

Models worth copying.

## Reference implementations

Three live implementations, all known-correct as of 2026-05-03:

- **Python** (operator/admin tooling):
  [`tools/stra2us_cli/client.py`](../tools/stra2us_cli/client.py).
  Uses `requests` for transport, `msgpack-python` for parsing, builtin
  `hmac` + `hashlib` for crypto. Reference for the cipher's plaintext
  byte-for-byte agreement test.

- **Particle C++** (Photon, Photon 2, Argon):
  `hal/particle/src/Stra2usClient.{h,cpp}` in critterchron. DeviceOS
  `TCPClient` for transport, hand-rolled msgpack header dispatch,
  hand-rolled HMAC-SHA256 (`hmac_sha256.{h,cpp}` — softer-resource
  Photon doesn't have a hardware-accelerated path).

- **ESP32 C++** (ESP32-C3, will work on S3/etc):
  `hal/esp32/src/Stra2usClient.{h,cpp}` in critterchron. Arduino-ESP32
  `WiFiClient` for transport, same hand-rolled msgpack and HMAC. The
  ESP32 path also implements `kv_fetch_stream_` for the firmware-OTA
  use case — a model for streaming-fetch implementations.

The Particle and ESP32 clients are mechanically diff-able — one is a
near-line-for-line port of the other with platform-specific includes
swapped. If you're writing a third C++ port (e.g. nRF52, RP2040), pick
the closer-architecturally one and start there.

## Validation checklist

Before declaring a new client correct:

- [ ] **Round-trip a small KV string.** Set via `stra2us set`, fetch
      via your client, compare byte-for-byte.
- [ ] **Round-trip a large KV value** (~10KB+ if your platform allows;
      ~1MB if you support streaming). Verifies that body framing,
      length-prefix handling, and HMAC-over-streamed-bytes all work.
- [ ] **Encrypted-value round-trip.** Set with `--encrypted`, fetch,
      verify plaintext matches.
- [ ] **Absent-key handling.** Fetch a key that doesn't exist. Should
      return a "no value" indication WITHOUT logging an error.
- [ ] **Drift rejection.** Manually skew the device clock by >300s
      and verify a 2xx response is rejected with an HMAC/drift error.
      (The server-side check covers requests; the client-side mirror
      covers responses.)
- [ ] **Tampered signature rejection.** Have a proxy mutate one byte
      of `X-Response-Signature` and verify the client fails closed.
- [ ] **Unsigned 2xx rejection.** Have a proxy strip the signing
      headers and verify the client fails closed.
- [ ] **Connection: close handling.** Run 100 sequential fetches and
      verify the client opens 100 fresh sockets (not reusing a
      half-closed one).
- [ ] **Concurrent render + network.** If your platform has a separate
      render loop, verify a long network call (queue consume on an
      empty topic, ~5s timeout) doesn't stall the render thread.
- [ ] **Heartbeat publish round-trip.** Publish a heartbeat, observe
      it land in the Stra2us admin UI, verify the response signing
      validates correctly on the device side.
- [ ] **Latency sparkline / equivalent.** Verify per-call timing
      shows up in operator-visible diagnostics (heartbeat field,
      cloud-side dashboard, whatever your fleet uses).

If a step fails, the existing reference impls are the troubleshooting
ground truth — diff your byte sequence against theirs and trace the
divergence.

## Things this document doesn't cover

- **Server-side ACL details** — see [`acl_model.md`](acl_model.md).
- **Admin UI behaviors** — those are server-side, not client-side.
- **Broadcasts** — see
  [`broadcast_architecture.md`](broadcast_architecture.md). No client
  implementation in critterchron yet.
- **WebSocket subscriptions** — none in the current Stra2us protocol.
- **Catalog UI / spec** — [`catalog_spec.md`](catalog_spec.md).

## Revision history

- 2026-05-03 — Initial draft, distilled from Python CLI + Particle +
  ESP32 implementations across the critterchron + stra2us repos.
