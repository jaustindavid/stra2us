"""HMAC-signed HTTP client for the stra2us server.

Mirrors the device-side signing protocol in client/src/IoTClient.cpp:

    Request sig:  HMAC_SHA256(secret_bytes, URI + body + timestamp_str)
    Headers:      X-Client-ID, X-Timestamp, X-Signature
    Response sig: HMAC_SHA256 over URI + response_body + resp_timestamp,
                  returned in X-Response-Signature / X-Response-Timestamp

KV values are msgpack-encoded. `put(key, value)` sends a msgpack
string / int / float as appropriate; `get(key)` returns the decoded
Python value, or None on 404.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import quote

import msgpack
import requests


CLOCK_DRIFT_SECONDS = 300  # matches server + device-client policy

# Per-key KV value encryption — must match server core/security.py.
KVENC_LABEL = b"stra2us-kvenc-v1"
KVENC_EXT_TYPE = 0x21


def _kvenc_xor(secret: bytes, nonce: int, data: bytes) -> bytes:
    """HMAC-SHA256 keystream XOR. nonce is uint32 BE; counter increments per
    32-byte block. Symmetric, so used for both encrypt and decrypt."""
    nonce_bytes = nonce.to_bytes(4, "big")
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        out.extend(hmac.new(
            secret,
            KVENC_LABEL + nonce_bytes + bytes([counter]),
            hashlib.sha256,
        ).digest())
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out[:len(data)]))


class Stra2usError(RuntimeError):
    """Any signing, transport, or server-error failure."""


@dataclass
class Stra2usClient:
    base_url: str           # "http://host:port", no trailing slash
    client_id: str
    secret_hex: str         # 64-char hex → 32-byte secret
    timeout: float = 10.0
    verify_response: bool = True

    def _secret_bytes(self) -> bytes:
        try:
            return bytes.fromhex(self.secret_hex)
        except ValueError as e:
            raise Stra2usError(f"secret_hex is not valid hex: {e}") from e

    def _sign(self, uri: str, body: bytes, ts: int) -> str:
        payload = uri.encode("utf-8") + body + str(ts).encode("utf-8")
        return hmac.new(self._secret_bytes(), payload, hashlib.sha256).hexdigest()

    def _verify_resp(self, uri: str, body: bytes, headers) -> None:
        if not self.verify_response:
            return
        ts = headers.get("X-Response-Timestamp")
        sig = headers.get("X-Response-Signature")
        if not ts or not sig:
            raise Stra2usError("response missing signing headers")
        now = int(time.time())
        if abs(now - int(ts)) > CLOCK_DRIFT_SECONDS:
            raise Stra2usError(
                f"response timestamp drift too large ({now - int(ts)}s)"
            )
        payload = uri.encode("utf-8") + body + ts.encode("utf-8")
        expected = hmac.new(
            self._secret_bytes(), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise Stra2usError("response signature mismatch")

    def _request(
        self,
        method: str,
        uri: str,
        body: bytes,
        content_type: str | None,
        extra_headers: dict | None = None,
    ) -> requests.Response:
        ts = int(time.time())
        sig = self._sign(uri, body, ts)
        headers = {
            "X-Client-ID": self.client_id,
            "X-Timestamp": str(ts),
            "X-Signature": sig,
        }
        if content_type:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update(extra_headers)
        try:
            r = requests.request(
                method,
                self.base_url + uri,
                data=body,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise Stra2usError(f"{method} {uri}: {e}") from e
        if 200 <= r.status_code < 300:
            self._verify_resp(uri, r.content, r.headers)
        return r

    @staticmethod
    def _kv_uri(key: str) -> str:
        # URL-encode each path segment so slashes stay as separators.
        return "/kv/" + "/".join(quote(p, safe="") for p in key.split("/"))

    def put(self, key: str, value, encrypted: bool = False) -> requests.Response:
        """POST /kv/<key>. msgpack-encodes `value` before sending.

        When `encrypted=True`, sends `X-Encrypted: 1` so the server stores
        the encrypted-flag sidecar; the value bytes themselves are still
        sent as plaintext over the request channel (writes are authenticated
        but not confidential — the FR's threat model is response sniffing).
        Bare `put()` without `encrypted=True` clears the flag, matching
        the server's "demote to plaintext" semantic.
        """
        body = msgpack.packb(value, use_bin_type=True)
        extra = {"X-Encrypted": "1"} if encrypted else None
        r = self._request(
            "POST", self._kv_uri(key), body, "application/x-msgpack",
            extra_headers=extra,
        )
        if not (200 <= r.status_code < 300):
            raise Stra2usError(
                f"POST {key} → {r.status_code}: {r.text[:200]}"
            )
        return r

    def delete(self, key: str) -> None:
        """DELETE /kv/<key>. Idempotent — succeeds whether the key existed."""
        r = self._request("DELETE", self._kv_uri(key), b"", None)
        if not (200 <= r.status_code < 300):
            raise Stra2usError(
                f"DELETE {key} → {r.status_code}: {r.text[:200]}"
            )

    def get(self, key: str):
        """GET /kv/<key>. Returns decoded value, or None on 404.

        Note: the server currently returns 200 with a msgpack-encoded
        `{"status": "not_found"}` for missing keys, not a 404. We treat
        that shape as None too so callers can test `if value is None:`
        uniformly regardless of which convention the server uses.
        """
        r = self._request("GET", self._kv_uri(key), b"", None)
        if r.status_code == 404:
            return None
        if not (200 <= r.status_code < 300):
            raise Stra2usError(
                f"GET {key} → {r.status_code}: {r.text[:200]}"
            )
        try:
            value = msgpack.unpackb(r.content, raw=False)
        except Exception as e:
            raise Stra2usError(f"GET {key}: invalid msgpack response: {e}") from e
        if isinstance(value, msgpack.ExtType) and value.code == KVENC_EXT_TYPE:
            # Encrypted record (see docs/fr_encrypted_values.md). Decrypt
            # using the response timestamp as nonce and return the plaintext
            # transparently — callers don't need to know it was encrypted.
            ts_hdr = r.headers.get("X-Response-Timestamp")
            if ts_hdr is None:
                raise Stra2usError(
                    f"GET {key}: encrypted response missing X-Response-Timestamp"
                )
            plaintext = _kvenc_xor(self._secret_bytes(), int(ts_hdr), value.data)
            try:
                return plaintext.decode("utf-8")
            except UnicodeDecodeError:
                return plaintext
        if isinstance(value, dict) and value.get("status") == "not_found":
            return None
        return value
