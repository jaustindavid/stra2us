from fastapi import APIRouter, Depends, HTTPException, Request, Response
from core.redis_client import get_redis_client
from core.security import sign_payload, kvenc_xor, KVENC_EXT_TYPE
import msgpack
from .dependencies import verify_device_request, check_acl
import time

router = APIRouter()

MSGPACK_MT = "application/x-msgpack"

def signed_response(context: dict, request: Request, body: bytes,
                    status_code: int = 200,
                    media_type: str = MSGPACK_MT) -> Response:
    """Wrap `body` in a Response and attach X-Response-{Timestamp,Signature}.

    Signature layout matches the request direction: HMAC-SHA256 over
    `URI + body + timestamp`, keyed by the requesting client's shared secret.
    A client that already holds its own secret can verify without any new
    key material. Empty-body responses (e.g. 204) still get signed over the
    URI + empty-bytes + timestamp so the caller can trust the status line.
    """
    ts = int(time.time())
    uri = str(request.url.path)
    sig = sign_payload(context["secret_hex"], uri, body, ts)
    headers = {
        "X-Response-Timestamp": str(ts),
        "X-Response-Signature": sig,
    }
    return Response(content=body, status_code=status_code,
                    media_type=media_type, headers=headers)

def signed_msgpack(context: dict, request: Request, obj,
                   status_code: int = 200) -> Response:
    return signed_response(context, request, msgpack.packb(obj),
                           status_code=status_code)


def signed_encrypted_response(context: dict, request: Request,
                              plaintext: bytes) -> Response:
    """GET response for an encrypted KV record. Encrypts `plaintext` with the
    HMAC-keystream cipher keyed by the caller's shared secret, using the
    response timestamp as nonce, and wraps the ciphertext in msgpack ext
    type 0x21. The signature still covers the full (ciphertext-bearing) body
    so authenticity holds independently of confidentiality."""
    ts = int(time.time())
    ciphertext = kvenc_xor(context["secret_hex"], ts, plaintext)
    body = msgpack.packb(msgpack.ExtType(KVENC_EXT_TYPE, ciphertext))
    uri = str(request.url.path)
    sig = sign_payload(context["secret_hex"], uri, body, ts)
    headers = {
        "X-Response-Timestamp": str(ts),
        "X-Response-Signature": sig,
    }
    return Response(content=body, status_code=200,
                    media_type=MSGPACK_MT, headers=headers)

@router.post("/q/{topic:path}")
async def publish_message(
    topic: str,
    request: Request,
    ttl: int = 3600,
    context: dict = Depends(verify_device_request)
):
    if ttl > 604800:
        ttl = 604800

    await check_acl(context, f"q/{topic}", mode="write")
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    
    if "text/plain" in content_type:
        try:
            # Wrap raw string in msgpack
            body = msgpack.packb(body.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid UTF-8 payload")
    else:
        try:
            # Validate existing msgpack
            if len(body) > 0:
                msgpack.unpackb(body)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid MessagePack payload")

    redis = get_redis_client()
    exp_time = int(time.time()) + ttl
    publisher_id = context["client_id"]

    # Store payload, expiry, and the authenticated publisher identity
    await redis.xadd(f"q:{topic}", {
        "payload": body,
        "exp": str(exp_time),
        "client_id": publisher_id,
    })
    # Global TTL to prevent abandoned topic memory leaks
    await redis.expire(f"q:{topic}", 604800)

    return signed_msgpack(context, request, {"status": "ok"})

@router.get("/q/{topic:path}")
async def consume_message(
    topic: str,
    request: Request,
    envelope: bool = False,
    context: dict = Depends(verify_device_request)
):
    """Consume the next message from a topic queue.

    When ?envelope=true, the response is a msgpack-packed dict:
      {"data": <decoded payload>, "client_id": "<publisher>", "received_at": <unix seconds>}
    When omitted or false, the raw msgpack payload bytes are returned (legacy behaviour).
    """
    await check_acl(context, f"q/{topic}", mode="read")
    redis = get_redis_client()
    consumer_id = context["client_id"]
    cursor_key = f"cursor:{consumer_id}:q:{topic}"

    last_id = await redis.get(cursor_key)
    if last_id is None:
        last_id = "0-0"
    elif isinstance(last_id, bytes):
        last_id = last_id.decode('utf-8')

    current_time = int(time.time())

    while True:
        messages = await redis.xread({f"q:{topic}": last_id}, count=50)

        if not messages:
            return signed_response(context, request, b"", status_code=204)

        stream_name, records = messages[0]

        for msg_id, fields in records:
            last_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id

            exp = int(fields[b"exp"])
            if current_time <= exp:
                await redis.set(cursor_key, last_id)

                raw_payload = fields[b"payload"]

                if not envelope:
                    return signed_response(context, request, raw_payload)

                # --- Envelope mode ---
                # Decode the stored payload so it becomes the `data` field value
                try:
                    decoded_data = msgpack.unpackb(raw_payload, raw=False)
                except Exception:
                    decoded_data = raw_payload  # pass raw bytes through if unparseable

                # received_at: Redis Stream IDs are "{unix_ms}-{seq}" — authoritative server time
                ms_str = last_id.split("-")[0]
                received_at = int(ms_str) // 1000

                publisher_id = fields.get(b"client_id", b"unknown")
                if isinstance(publisher_id, bytes):
                    publisher_id = publisher_id.decode("utf-8")

                wrapped = msgpack.packb({
                    "data": decoded_data,
                    "client_id": publisher_id,
                    "received_at": received_at,
                }, use_bin_type=True)
                return signed_response(context, request, wrapped)

        # advance cursor and keep polling if all current batch were expired
        await redis.set(cursor_key, last_id)

@router.post("/kv/{key:path}")
async def write_kv(
    key: str,
    request: Request,
    context: dict = Depends(verify_device_request)
):
    await check_acl(context, f"kv/{key}", mode="write")
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    
    if "text/plain" in content_type:
        try:
            body = msgpack.packb(body.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid UTF-8 payload")
    else:
        try:
            if len(body) > 0:
                msgpack.unpackb(body)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid MessagePack payload")

    redis = get_redis_client()
    await redis.set(f"kv:{key}", body)
    # Encrypted-flag sidecar (see docs/fr_encrypted_values.md). Bare set
    # without the header demotes a previously-encrypted record to plaintext.
    if request.headers.get("X-Encrypted") == "1":
        await redis.set(f"kv:{key}:enc", b"1")
    else:
        await redis.delete(f"kv:{key}:enc")

    return signed_msgpack(context, request, {"status": "ok"})

@router.get("/kv/{key:path}")
async def read_kv(
    key: str,
    request: Request,
    context: dict = Depends(verify_device_request)
):
    await check_acl(context, f"kv/{key}", mode="read")
    redis = get_redis_client()
    val = await redis.get(f"kv:{key}")

    # Stash hit-vs-miss for the activity-log middleware. Wire response
    # is 200 in either case (devices treat `{"status":"not_found"}` as
    # "fall back to default" without 404 handling), so the middleware
    # can't tell them apart from the status alone — this hint is what
    # makes the activity-log "Miss (200)" vs "Hit (200)" entries
    # distinguishable. See docs/admin_ui_todo.md.
    request.state.kv_hit = val is not None

    if val is None:
        return signed_msgpack(context, request, {"status": "not_found"})

    if await redis.get(f"kv:{key}:enc"):
        # Encrypted record: unwrap the inner str/bin payload, encrypt the
        # raw bytes under the response timestamp, and ship as ext 0x21.
        # The msgpack-shape (str vs bin and length-class) is dropped on the
        # wire; the consumer recovers type from catalog/context.
        try:
            inner = msgpack.unpackb(val, raw=True)
        except Exception:
            raise HTTPException(status_code=500,
                                detail="Encrypted record: stored value is not msgpack")
        if isinstance(inner, (bytes, bytearray)):
            plaintext = bytes(inner)
        elif isinstance(inner, str):
            plaintext = inner.encode("utf-8")
        else:
            raise HTTPException(status_code=500,
                                detail="Encrypted record: stored value is not str/bin")
        return signed_encrypted_response(context, request, plaintext)

    return signed_response(context, request, val)

@router.delete("/kv/{key:path}")
async def delete_kv(
    key: str,
    request: Request,
    context: dict = Depends(verify_device_request)
):
    """Idempotent: succeeds whether or not the key existed. Mirrors the
    admin-side DELETE; the device path was added so HMAC clients (e.g.
    a publish-tool wanting to retract a script blob) can clear keys
    without holding an admin credential."""
    await check_acl(context, f"kv/{key}", mode="write")
    redis = get_redis_client()
    await redis.delete(f"kv:{key}", f"kv:{key}:enc")
    return signed_msgpack(context, request, {"status": "ok"})
