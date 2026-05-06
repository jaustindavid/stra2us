# Local development — running the live test suite

The `tools/tests/test_*_live.py` suites are gated on a reachable
stra2us instance with a provisioned test client. This doc captures
the host-side bring-up dance for those tests, *not* the docker-based
staging environment (which lives in
[`docs/staging_environment.md`](staging_environment.md)).

This file was previously named `docs/staging.md`; renamed for
clarity since "staging" now refers to the docker stack.

## One-time prerequisites

- Redis running locally (`redis-cli ping` → `PONG`).
- `backend/venv` populated (`cd backend && python3 -m venv venv && venv/bin/pip install -r requirements.txt`).
- `tools/venv` populated similarly against `tools/`.

## Bring up the backend

The `start.sh` and `supervisord.conf` paths assume the docker layout
(`/firmware`, `/app`). For a host-side run, override the firmware dir
and set `PYTHONPATH` directly:

```bash
cd backend
mkdir -p /tmp/stra2us_firmware
PYTHONPATH=src STRA2US_FIRMWARE_DIR=/tmp/stra2us_firmware \
  ./venv/bin/uvicorn src.main:app --host 127.0.0.1 --port 8153 \
  > /tmp/stra2us_uvicorn.log 2>&1 &
echo $! > /tmp/stra2us_uvicorn.pid

# Wait for ready:
until curl -sf http://127.0.0.1:8153/docs > /dev/null; do sleep 1; done
```

### Gotcha: `system:activity_log` type mismatch

The activity-log middleware does `XADD system:activity_log` — i.e. it
expects a Redis **stream**. Older code paths used a list under the same
key. If your local Redis still holds it as a list, *every* `/kv/*` and
`/q/*` request 500s with `WRONGTYPE`. Check + clear:

```bash
redis-cli type system:activity_log     # → "stream" or "none" is fine
redis-cli del system:activity_log      # only if it's "list" or "string"
```

Worth checking against any environment after a long upgrade gap, not
just local.

## Provision a test client

The live tests need `STRA2US_HOST` / `STRA2US_CLIENT_ID` /
`STRA2US_SECRET_HEX`. Bypass the admin UI and write the client
straight into Redis — `dependencies.py:verify_device_request` reads
exactly these two keys:

```bash
CLIENT_ID="staging_test_$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
SECRET_HEX=$(python3 -c "import secrets; print(secrets.token_hex(32))")

redis-cli set "client:${CLIENT_ID}:secret" "$SECRET_HEX"
redis-cli set "client:${CLIENT_ID}:acl" \
  '{"permissions":[{"prefix":"_test","access":"rw"},{"prefix":"_catalog","access":"rw"}]}'
```

ACL prefixes:
- `_test` — what `test_encrypted_live.py` writes under (configurable
  via `TEST_PREFIX` in that file).
- `_catalog` — what `test_publish_live.py` writes under.

Add other prefixes if you wire up new live tests.

## Run the tests

```bash
cd tools
STRA2US_HOST=http://127.0.0.1:8153 \
  STRA2US_CLIENT_ID="$CLIENT_ID" \
  STRA2US_SECRET_HEX="$SECRET_HEX" \
  ./venv/bin/python -m pytest tests/ -v
```

Without the env triple set, the live suites skip cleanly — that's
how CI ignores them.

## Tear down

```bash
kill $(cat /tmp/stra2us_uvicorn.pid)
redis-cli del "client:${CLIENT_ID}:secret" "client:${CLIENT_ID}:acl"

# Sweep any test KV keys left if a teardown failed mid-test:
redis-cli --scan --pattern 'kv:_test/*' | xargs -r -I{} redis-cli del {}
```

The tests use `try/finally client.delete(key)` for cleanup, but a
crash between `put` and `delete` can leave keys behind — the sweep
above is a belt-and-suspenders.

---

# Verifying the admin UI in a browser

The live test suite covers the device-facing HTTP API but says nothing
about the admin dashboard. For UI changes (lock badges, edit-modal
checkboxes, the peek modal — see [admin_ui_todo.md](admin_ui_todo.md)),
the path below brings the backend up under `.claude/launch.json` so a
preview browser can drive it.

## Heads-up: db 15, not db 0

`.claude/launch.json` sets `REDIS_URL=redis://localhost:6379/15`, so
this path is **isolated from the live-test path above** (which uses the
default db 0 via plain `redis-cli`). Pick one or the other per session,
or remember to pass `-n 15` to `redis-cli` for every command in this
section.

## Provision a preview admin

Basic auth is htpasswd-backed, ACLs are in Redis. Add a known user
without disturbing the existing `admin:` entry:

```bash
cd backend
./venv/bin/python create_admin.py preview previewpass   # additive — only replaces matching usernames
redis-cli -n 15 set 'admin_acls:preview' \
  '{"permissions":[{"prefix":"*","access":"rw"}]}'
```

## Seed at least one of each KV shape

So the rendered list has something to look at, including an encrypted
record that exercises the lock badge path:

```bash
./venv/bin/python -c "
import redis, msgpack
r = redis.from_url('redis://localhost:6379/15')
r.set('kv:demo/heartbeep',         msgpack.packb(30))
r.set('kv:demo/wifi_password',     msgpack.packb('hunter2-secret'))
r.set('kv:demo/wifi_password:enc', b'1')   # the sidecar that flips encryption on
"
```

Use the venv python — system python often lacks `msgpack`, and a silent
import failure here writes empty values into Redis that are hard to
diagnose later.

## Drive the UI

Start the backend through the launch config (in a Claude Code session,
`preview_start` reuses the running server if any), then navigate to
`http://127.0.0.1:8153/admin/` with basic auth `preview / previewpass`.

Browser quirk worth knowing: navigating with `http://user:pass@host/...`
in the URL caches the basic-auth credentials but also taints the page
origin so subsequent `fetch('/api/admin/stats')` calls fail with
*"Request cannot be constructed from a URL that includes credentials."*
Workaround: do a `window.location.replace('http://127.0.0.1:8153/admin/')`
after the initial credentialed nav, or trigger basic auth via the
browser's prompt instead of embedding creds in the URL.

## What's worth checking on the rendered UI

For any KV/encryption-flag UI change, the round-trip below catches
nearly every category of regression in one pass:

1. Stats shows the right number of rows and **no** phantom `kv:foo:enc`
   entries (the sidecar filter in `routes_admin.py:get_stats`).
2. The encrypted seed (`demo/wifi_password`) renders the 🔒 badge; the
   plaintext one (`demo/heartbeep`) does not.
3. Open Edit on the encrypted record → checkbox is **pre-filled**
   (regression guard against silent demote-on-save).
4. Open Edit on a plaintext record → checkbox is **clear**.
5. Tick the box on a plaintext record, save, confirm `/api/admin/stats`
   now reports `encrypted: true` for it and the rendered list shows
   the badge.
6. Untick on the now-encrypted record, save, confirm it demotes.
7. Peek modal first line reads `Encrypted: yes/no` and still shows the
   plaintext value (admin holds the keys; encryption only applies on
   the device-facing GET path).

Steps 3 and 5–6 are the most valuable — they catch the modal-pre-fill
bug and the POST-payload-shape bug, which are the two ways the FR's
"demote on bare set" semantic can become a UI footgun.

## Tear down (preview path)

```bash
redis-cli -n 15 del kv:demo/heartbeep kv:demo/wifi_password kv:demo/wifi_password:enc admin_acls:preview
# Remove the preview user from htpasswd (leaves other entries intact):
python3 -c "
with open('backend/admin.htpasswd') as f: lines = f.readlines()
with open('backend/admin.htpasswd', 'w') as f:
    for l in lines:
        if not l.startswith('preview:'): f.write(l)
"
```

The launch-config server is stopped via `preview_stop` (or just kill
the uvicorn PID); it owns no state outside Redis db 15 + htpasswd.
