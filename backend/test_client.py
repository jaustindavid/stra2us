import argparse
import time
import hmac
import hashlib
import msgpack
import httpx
import json
import logging
import sys

# Configure logging
#
# ==========================================
# TEST CLIENT USAGE EXAMPLES
# ==========================================
# 
# First, create a new client and grab the Client ID and Secret (Hex) from:
# http://127.0.0.1:8000/admin
# 
# 1. Write Config Values (set)
#    python test_client.py --client-id <YOUR_CLIENT_ID> --secret <YOUR_SECRET> set device-config-1 "{\"sleep_interval\": 60, \"retries\": 3}"
#
# 2. Get Config Values (get)
#    python test_client.py --client-id <YOUR_CLIENT_ID> --secret <YOUR_SECRET> get device-config-1
#
# 3. Follow a Topic (follow) - Run this in a separate terminal tab
#    python test_client.py --client-id <YOUR_CLIENT_ID> --secret <YOUR_SECRET> follow "telemetry/sensor-data"
#
# 4. Publish to a Topic (publish)
#    python test_client.py --client-id <YOUR_CLIENT_ID> --secret <YOUR_SECRET> publish "telemetry/sensor-data" "{\"temp\": 22.4, \"humidity\": 48}"
# ==========================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_URL = "http://127.0.0.1:8000"

def generate_signature(secret_hex, uri, body, timestamp):
    secret_bytes = bytes.fromhex(secret_hex)
    payload = uri.encode('utf-8') + body + str(timestamp).encode('utf-8')
    return hmac.new(secret_bytes, payload, hashlib.sha256).hexdigest()

# --- Response signature verification ---
# The server signs `/kv/*` and `/q/*` responses with `X-Response-Signature`
# (HMAC-SHA256 over URI + body + ts, keyed by the same shared secret the
# client used to sign its request). Missing headers are treated as a failure
# unless `--no-verify-response` is passed, which lets us talk to pre-signing
# servers during the rollout window.

def verify_response(resp, uri, secret_hex, enforce, max_drift=300):
    ts_hdr  = resp.headers.get("X-Response-Timestamp")
    sig_hdr = resp.headers.get("X-Response-Signature")

    if ts_hdr is None or sig_hdr is None:
        if enforce:
            logging.error(
                "Response missing signature headers "
                f"(X-Response-Timestamp={ts_hdr!r}, X-Response-Signature={sig_hdr!r}). "
                "Server may be pre-signing; pass --no-verify-response to skip."
            )
            sys.exit(2)
        return

    try:
        ts = int(ts_hdr)
    except ValueError:
        logging.error(f"Malformed X-Response-Timestamp: {ts_hdr!r}")
        sys.exit(2)

    now = int(time.time())
    if abs(now - ts) > max_drift:
        logging.error(f"Response timestamp drift {now - ts}s exceeds {max_drift}s — possible replay.")
        sys.exit(2)

    expected = generate_signature(secret_hex, uri, resp.content, ts)
    if not hmac.compare_digest(expected, sig_hdr):
        logging.error(
            f"Response signature mismatch on {uri}. "
            f"expected={expected[:16]}… got={sig_hdr[:16]}…"
        )
        sys.exit(2)

def make_request(method, path, body_data, client_id, secret, base_url,
                 params=None, verify_response_sig=True):
    uri = f"/{path}"

    if body_data is not None:
        body = msgpack.packb(body_data)
    else:
        body = b""

    timestamp = int(time.time())
    signature = generate_signature(secret, uri, body, timestamp)

    headers = {
        "X-Client-ID": client_id,
        "X-Timestamp": str(timestamp),
        "X-Signature": signature,
        "Content-Type": "application/x-msgpack"
    }

    # Ensure URL doesn't have trailing slash if path has leading one
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        with httpx.Client(timeout=10.0) as client:
            if method == "POST":
                resp = client.post(url, params=params, content=body, headers=headers)
            elif method == "GET":
                resp = client.get(url, params=params, headers=headers)
            else:
                raise ValueError(f"unsupported method {method}")
    except httpx.ConnectError:
        logging.error(f"FATAL: Could not connect to {base_url}.")
        if "127.0.0.1" in base_url or "localhost" in base_url:
            logging.error("If your server is on a Raspberry Pi, use --url http://<RPI_IP>:8000")
        sys.exit(1)
    except httpx.RequestError as e:
        logging.error(f"Request failed: {e}")
        sys.exit(1)

    # Only verify on success-class responses — 4xx errors from `verify_device_request`
    # are raised before `signed_response` runs and therefore won't carry the headers.
    if 200 <= resp.status_code < 300:
        verify_response(resp, uri, secret, enforce=verify_response_sig)
    return resp

def try_parse_data(text):
    try:
        return json.loads(text)
    except:
        return text

def parse_response(resp):
    if not resp.content:
        return None
    try:
        return msgpack.unpackb(resp.content)
    except:
        return resp.content

def command_publish(args):
    data = try_parse_data(args.data)
    params = {}
    if args.ttl is not None:
        params["ttl"] = args.ttl
    resp = make_request("POST", f"q/{args.topic}", data, args.client_id, args.secret, args.url,
                        params=params, verify_response_sig=not args.no_verify_response)
    
    if resp.status_code == 200:
        logging.info(f"Published to {args.topic}: {data}")
    else:
        logging.error(f"Failed to publish: {resp.status_code} - {resp.text}")

def format_envelope(msg):
    """Pretty-print an envelope response dict."""
    if not isinstance(msg, dict):
        return str(msg)
    client_id  = msg.get('client_id', msg.get(b'client_id', '?'))
    received_at = msg.get('received_at', msg.get(b'received_at', 0))
    data       = msg.get('data', msg.get(b'data', msg))
    if isinstance(client_id, bytes):  client_id   = client_id.decode()
    if isinstance(data, bytes):       data        = data.decode('utf-8', errors='replace')
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(received_at))
    return f"[{ts}] from={client_id}  data={data!r}"

def command_follow(args):
    delay = args.delay if hasattr(args, "delay") and args.delay else 1.0
    envelope = getattr(args, "envelope", False)
    params = {"envelope": "true"} if envelope else {}
    mode = "envelope" if envelope else "raw"
    logging.info(f"Following topic: {args.topic} (mode={mode}, poll every {delay}s)")
    try:
        while True:
            # Drain the queue fully before sleeping
            while True:
                resp = make_request("GET", f"q/{args.topic}", None, args.client_id, args.secret, args.url,
                                    params=params, verify_response_sig=not args.no_verify_response)

                if resp.status_code == 200:
                    msg = parse_response(resp)
                    if envelope:
                        logging.info(format_envelope(msg))
                    else:
                        logging.info(f"Received: {msg}")
                elif resp.status_code == 204:
                    break  # Queue is currently empty, break to wait
                else:
                    logging.error(f"Failed to fetch: {resp.status_code} - {resp.text}")
                    break

            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nStopped.")

def command_set(args):
    data = try_parse_data(args.value)
    resp = make_request("POST", f"kv/{args.key}", data, args.client_id, args.secret, args.url,
                        verify_response_sig=not args.no_verify_response)
    
    if resp.status_code == 200:
        logging.info(f"Set {args.key} = {data}")
    else:
        logging.error(f"Failed to set: {resp.status_code} - {resp.text}")

def command_get(args):
    resp = make_request("GET", f"kv/{args.key}", None, args.client_id, args.secret, args.url,
                        verify_response_sig=not args.no_verify_response)
    
    if resp.status_code == 200:
        msg = parse_response(resp)
        if isinstance(msg, dict) and msg.get("status") == "not_found":
            logging.info(f"Key '{args.key}' not found.")
        else:
            logging.info(f"{args.key} = {msg}")
    else:
        logging.error(f"Failed to get: {resp.status_code} - {resp.text}")


def main():
    parser = argparse.ArgumentParser(description="IoT Messaging Service Client (Test CLI)")
    parser.add_argument("--client-id", required=True, help="Registered Client ID")
    parser.add_argument("--secret", required=True, help="Client Secret (Hex)")
    parser.add_argument("--url", default=BASE_URL, help="Base URL of the service")
    parser.add_argument("--no-verify-response", action="store_true",
                        help="Skip X-Response-Signature verification (for talking to pre-signing servers).")

    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Publish Command
    publish_parser = subparsers.add_parser("publish", help="Publish a message to a topic queue")
    publish_parser.add_argument("topic", help="Topic to publish to")
    publish_parser.add_argument("data", help="Data payload (JSON or string)")
    publish_parser.add_argument("--ttl", type=int, help="Optional TTL in seconds (default 3600)")
    
    # Follow Command
    follow_parser = subparsers.add_parser("follow", help="Consume messages from a topic in a loop")
    follow_parser.add_argument("topic", help="Topic to follow")
    follow_parser.add_argument("--delay", type=float, default=1.0, help="Polling delay in seconds (default 1.0)")
    follow_parser.add_argument("--envelope", action="store_true", help="Request server-side attribution metadata (client_id, received_at)")

    # Set Command
    set_parser = subparsers.add_parser("set", help="Set a KV config pair")
    set_parser.add_argument("key", help="Key name")
    set_parser.add_argument("value", help="Value (JSON or string)")
    
    # Get Command
    get_parser = subparsers.add_parser("get", help="Read a KV config pair")
    get_parser.add_argument("key", help="Key name")

    args = parser.parse_args()

    if args.command == "publish":
        command_publish(args)
    elif args.command == "follow":
        command_follow(args)
    elif args.command == "set":
        command_set(args)
    elif args.command == "get":
        command_get(args)

if __name__ == "__main__":
    main()
