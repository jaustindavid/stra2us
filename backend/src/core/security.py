import secrets
import hmac
import hashlib
import time

def generate_secret() -> str:
    """Generate a unique 32-byte shared secret (hex encoded for easy distribution)."""
    return secrets.token_hex(32)

def calculate_signature(secret_hex: str, payload: bytes, timestamp: int) -> str:
    """
    Calculate the HMAC-SHA256 signature for the given payload and timestamp.
    The payload already includes the URI if we pass it concatenated,
    but based on our plan, we'll hash the URI + Body + Timestamp.
    So this function just takes a pre-concatenated bytes buffer.
    """
    secret_bytes = bytes.fromhex(secret_hex)
    return hmac.new(secret_bytes, payload, hashlib.sha256).hexdigest()

def sign_payload(secret_hex: str, uri: str, body: bytes, timestamp: int) -> str:
    """HMAC-SHA256 over URI + body + timestamp. Shared by request-signing
    (client→server) and response-signing (server→client) so both directions
    use byte-identical concatenation order."""
    payload = uri.encode('utf-8') + body + str(timestamp).encode('utf-8')
    return calculate_signature(secret_hex, payload, timestamp)

def verify_signature(secret_hex: str, uri: str, body: bytes, timestamp: int, signature: str) -> bool:
    """
    Verify the signature using a constant-time comparison.
    HMAC over URI + Body + Timestamp.
    """
    expected_mac = sign_payload(secret_hex, uri, body, timestamp)
    return hmac.compare_digest(expected_mac, signature)

def verify_timestamp(timestamp: int, max_drift: int = 300) -> bool:
    """
    Replay mitigation: Ensure timestamp is within the max_drift
    """
    current_time = int(time.time())
    return abs(current_time - timestamp) <= max_drift


# Per-key KV value encryption (see docs/fr_encrypted_values.md).
# Domain-separation label keeps this keystream from colliding with any
# other HMAC use of the same per-client secret.
KVENC_LABEL = b"stra2us-kvenc-v1"
KVENC_EXT_TYPE = 0x21


def kvenc_keystream(secret_hex: str, nonce: int, length: int) -> bytes:
    """HMAC-SHA256 stream cipher keystream. `nonce` is the response timestamp
    (uint32 BE); `counter` increments per 32-byte block until enough bytes."""
    secret = bytes.fromhex(secret_hex)
    nonce_bytes = nonce.to_bytes(4, "big")
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            secret,
            KVENC_LABEL + nonce_bytes + bytes([counter]),
            hashlib.sha256,
        ).digest()
        out.extend(block)
        counter += 1
        if counter > 255:
            # 256 * 32 = 8 KiB — well past any plausible KV value.
            raise ValueError("kvenc plaintext too long for 1-byte counter")
    return bytes(out[:length])


def kvenc_xor(secret_hex: str, nonce: int, data: bytes) -> bytes:
    """Encrypt or decrypt (XOR is symmetric) `data` with the keystream."""
    ks = kvenc_keystream(secret_hex, nonce, len(data))
    return bytes(a ^ b for a, b in zip(data, ks))
