# Staging Environment — spec

## Goals

A pre-production environment that runs the same code paths as prod
in a container layout that mirrors prod's, so we can validate
changes (dep bumps, code edits, middleware changes) before they
touch the live `stra2us.austindavid.com` hostname or any real
device traffic. Aligned with the [Rules of Operation](../README.md):
every change ships through staging first, smoke-test green is the
gate, and the dev → staging → prod flow has named verified-working
targets at each step.

## Non-goals

- Multi-host orchestration. Staging runs on the same docker host
  as prod (the host has plenty of RAM and storage). If we ever
  outgrow that, the spec changes.
- Highly-available staging. If staging's tunnel flaps, that's
  diagnostic information, not an outage.
- Shared state between prod and staging. They have separate Redis
  volumes, separate firmware dirs, separate everything except the
  underlying docker daemon and the host kernel.

## Topology

```
PROD                                       STAGING
─────────────────────────────────────────  ─────────────────────────────────────────
Real IoT devices                           Synthetic device probe (sidecar)
   │                                          │
   │ HTTP, port 8153, HMAC-signed              │ HTTP, port 8253, HMAC-signed
   ▼                                          ▼
iot.stra2us.austindavid.com ── A ──┐       iot-staging.stra2us.austindavid.com ── A ──┐
                                   │                                                  │
                                   ▼                                                  ▼
                          stra2us-iot (prod)                                stra2us-iot-staging
                                   ▲                                                  ▲
                                   │ docker net (prod)                                │ docker net (staging)
                                   │                                                  │
            stra2us-cloudflared ───┘                  stra2us-cloudflared-staging ────┘
                  ▲                                                  ▲
                  │ HTTPS, port 443                                  │ HTTPS, port 443
                  │                                                  │
stra2us.austindavid.com ── CNAME ─┘                staging.stra2us.austindavid.com ── CNAME ┘
                  ▲                                                  ▲
                  │                                                  │
              Browsers                                           Browsers (testing)
```

Two completely separate vertical slices on the same host. Container
names disambiguate (`stra2us-iot-staging`, `stra2us-cloudflared-staging`).
Ports differ on the device path (8153 prod, 8253 staging) so that a
misconfigured DNS or curl probe can't accidentally cross. Internal
docker networks are separate (prod and staging compose stacks each
get their own default network).

Hostnames:
- `stra2us.austindavid.com` — prod browser path (CF tunnel A).
- `iot.stra2us.austindavid.com` — prod device path (A record, :8153).
- `staging.stra2us.austindavid.com` — staging browser path (CF tunnel B).
- `iot-staging.stra2us.austindavid.com` — staging device path
  (A record to same public IP, :8253).

DNS for the two staging names is set up once in CF dashboard and
never moves. The prod DNS is unchanged from today.

## File layout

```
docker-compose.yaml              # prod (existing)
docker-compose.staging.yaml      # new — staging stack

backend/Dockerfile               # base image (existing)
                                 #   gains an ARG INCLUDE_DEBUG_TOOLS
                                 #   that conditionally installs
                                 #   tcpdump/telnet/etc.

tools/stage                      # helper script — `tools/stage <cmd>`
tools/stra2us_cli/               # existing Python client; the
                                 #   short-lived synthetic-traffic
                                 #   job will be added here (see
                                 #   TODO). Real LAN devices are
                                 #   the primary device-traffic
                                 #   source.
tools/smoke_test.sh              # unchanged — staging targeting
                                 #   handled via the existing env-var
                                 #   overrides

.env                             # prod (existing)
.env.staging                     # new — staging-specific creds and
                                 #   tunnel token. Same shape as .env;
                                 #   different values.
```

## Container image — debug tooling

The existing `backend/Dockerfile` gets an `ARG`-driven optional layer:

```dockerfile
ARG INCLUDE_DEBUG_TOOLS=0
RUN if [ "$INCLUDE_DEBUG_TOOLS" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            tcpdump telnet netcat-openbsd dnsutils iputils-ping \
            procps strace lsof jq vim-tiny redis-tools \
        && rm -rf /var/lib/apt/lists/*; \
    fi
```

`docker-compose.staging.yaml` builds with `INCLUDE_DEBUG_TOOLS=1`;
prod's compose leaves it at the default 0 so the prod image stays
lean. Same Dockerfile, same source-of-truth, no copy-paste drift.

Tools to expect in the staging container:
- `tcpdump` — sniff inside the container's network namespace
- `telnet`, `nc` (netcat-openbsd) — manual TCP probes
- `dig`, `host`, `nslookup` (dnsutils) — DNS sanity
- `ping` (iputils-ping)
- `ps`, `top` (procps)
- `strace`, `lsof` — process/file-descriptor inspection
- `jq` — JSON manipulation
- `vim-tiny` — minimal editor for fixing config in-container
- `redis-cli` (redis-tools) — already needed; convenient

## Device traffic on staging

The smoke test's hard checkpoint is "real device traffic in the
activity log within 60 seconds." Staging needs traffic. We use two
sources, complementing each other:

### (a) Real LAN-only staging devices

The primary source. 1–2 sacrificial devices configured to point at
`iot-staging.stra2us.austindavid.com:8253` with high heartbeat
rates. The hostname's DNS is set up; the device path is **not**
exposed to the public internet — it stays LAN-only. Real devices
exercise real msgpack bodies, real HMAC signing, real clock-skew
behavior, real connection-reset patterns — full prod fidelity for
the device path with no internet attack surface added.

**Why we can't tunnel the device path through CF:** the C++ device
clients are HTTP-only and have no TLS/SSL stack. CF's edge forces
HTTPS at the public boundary. So the staging device path mirrors
prod's: direct HTTP/8253 to the host, no CF in the path.

**Implication for smoke testing:** the device-path checks in
`tools/stage smoke` must run from the LAN (the docker host itself
is fine). An external smoke run (e.g. from an off-network box)
would have to skip device-path checks. Future scope: a
`--skip-device` flag on the smoke test for that case.

### (c) Short-lived synthetic traffic via the existing Python client

For ad-hoc bursts — running `tools/stage smoke` when no real device
happens to be heartbeating, validating a code change that touches
device-path handling, generating load — there's an action item to
build a small CLI on top of the existing `tools/stra2us_cli`
(Python client) that posts signed device traffic to a target host
for a configured duration. See [TODO.md](../TODO.md). This is
complementary to (a), not a replacement: real devices are the
source of truth, the synthetic job is an on-demand top-up.

## Promotion flow: dev → staging → prod

Three named environments with explicit promotion:

### dev → staging

1. Developer commits and pushes to a feature branch (or main).
2. On the deploy host:
   ```sh
   tools/stage deploy <branch-or-sha>
   ```
   Which under the hood does:
   ```sh
   git fetch
   git checkout -B staging-current <ref>
   docker compose -f docker-compose.staging.yaml build stra2us-iot-staging
   docker compose -f docker-compose.staging.yaml up -d
   tools/stage wait-tunnel    # poll cloudflared logs for connIndex=3
   tools/stage smoke          # smoke test against staging hostnames
   ```
3. If smoke is green, staging is at "verified-working" for that ref.
   If red, the smoke output names what's wrong; iterate.

### staging → prod

Once staging is green and the operator has done a UI eyeball, the
ref gets a tag and prod is re-pointed at the tag.

1. From any machine with push access to the repo:
   ```sh
   git tag -a v1.X.Y <sha-that-was-on-staging> \
     -m "what changed since the previous tag"
   git push origin v1.X.Y
   ```
2. On the deploy host, promote prod:
   ```sh
   tools/stage promote v1.X.Y
   ```
   Which does:
   ```sh
   git fetch --tags
   git checkout -B deploy v1.X.Y
   docker compose -f docker-compose.yaml build stra2us-iot
   docker compose -f docker-compose.yaml up -d
   tools/stage wait-tunnel-prod
   tools/stage smoke-prod
   ```
3. Smoke-test result is the prod checkpoint. Plus a UI eyeball if
   the change touched UI surfaces (Rule 9).

### Terminology — tag, branch, "sync"

- **Tag** — a named, immutable pointer to a specific commit. Created
  with `git tag -a <name> <sha>`. The right primitive for a deploy
  target. Correct usage in this flow.
- **Branch** — a moving pointer that follows commits. The `deploy`
  branch is the prod pointer; it's repointed at a new tag at every
  promotion. The `staging-current` branch is the staging pointer.
- **"Sync"** — colloquial; in git this is two operations:
  `git fetch` (download new refs from origin) followed by
  `git checkout` (move HEAD to the desired ref). The `tools/stage`
  helper wraps both into a single command for both staging and
  prod, so the operator never has to remember which is which.

Prod is "always pointed at the most recent tag the operator has
promoted" — exactly the "pull from head [of the deploy branch,
which is at a tag]" model you described.

## Helper script — `tools/stage`

Single-entry-point script in `tools/stage` (executable bash). All
subcommands operate on staging unless explicitly named for prod.

Subcommands:

| Command | Effect |
|---|---|
| `tools/stage up` | `docker compose -f docker-compose.staging.yaml up -d` |
| `tools/stage down` | Stop and remove staging containers |
| `tools/stage restart [service]` | Restart all or one staging service |
| `tools/stage rebuild [service]` | `build` + `up -d` for a service |
| `tools/stage bash [service]` | `docker compose exec <service> bash` (default service: `stra2us-iot-staging`) |
| `tools/stage logs [service]` | Tail logs |
| `tools/stage wait-tunnel` | Poll cloudflared logs until `connIndex=3` registers |
| `tools/stage smoke` | Run smoke test with staging env vars |
| `tools/stage deploy <ref>` | Full dev→staging flow (checkout, rebuild, wait, smoke) |
| `tools/stage promote <tag>` | Full staging→prod flow (tag, prod checkout, rebuild, wait, smoke) |
| `tools/stage smoke-prod` | Run smoke test against prod hostnames |
| `tools/stage status` | Print container states + last-deployed ref for both stacks |

Every subcommand prints what it's doing before doing it (so the
operator can see exactly which `docker compose ...` invocation ran)
and exits nonzero on failure.

The script reads `.env` for prod and `.env.staging` for staging.
Neither file is checked in.

## Smoke test integration

`tools/smoke_test.sh` is unchanged. The helper script invokes it
with the right env-var overrides:

```sh
# inside tools/stage smoke
( set -a && source .env.staging && \
  STRA2US_BROWSER_HOST=staging.stra2us.austindavid.com \
  STRA2US_DEVICE_HOST=iot-staging.stra2us.austindavid.com \
  STRA2US_DEVICE_PORT=8253 \
  tools/smoke_test.sh )
```

Same for prod via `.env`. The smoke test's exit code is the
checkpoint signal; nonzero halts whatever invoked it (e.g.
`tools/stage deploy` aborts before promoting).

## Implementation order

1. **Add `INCLUDE_DEBUG_TOOLS` ARG to `backend/Dockerfile`.** Verify
   prod build still works (no behavior change at default).
2. **Write `docker-compose.staging.yaml`.** Mirrors prod's compose
   with separate container names, ports, volumes, and the
   `cloudflared-staging` service pointing at a new CF tunnel.
3. **Set up DNS + CF tunnel for staging.** Create the staging
   tunnel in CF dashboard, two new DNS records, populate
   `.env.staging` with the tunnel token.
4. **Write `tools/stage`.** Start with `up`/`down`/`bash`/`logs`/
   `smoke`; add `deploy`/`promote` once the basics work.
5. **Spin up 1–2 LAN-only staging devices.** Configure them to
   point at `iot-staging.stra2us.austindavid.com:8253` with a high
   heartbeat rate. Confirm their traffic shows up in the staging
   activity log. (Synthetic-traffic CLI is a separate AI; not
   required for first staging cutover.)
6. **First end-to-end test.** Use `tools/stage deploy <main-sha>`
   to put the current main on staging; smoke-test green; eyeball
   the staging admin UI.
7. **Use `tools/stage promote` once.** Promote the staging-verified
   commit to prod via tag, watch the prod smoke test go green,
   confirm devices unaffected throughout.
8. **Document.** Add a "deploy" section to README.md pointing at
   this spec; remove the manual `docker compose build && up -d`
   muscle memory in favor of `tools/stage`.

## Decisions made (formerly open questions)

### Seed users on staging

Staging users are independent of prod — no shared IDs required.
At minimum, three seeded identities:

| User | Auth | Role | Why seeded |
|---|---|---|---|
| `smoke` | htpasswd + `admin_acls:smoke` with `*:rw` | smoke-test client | The smoke test's activity-log heartbeat check needs an account that can read the full log. Wildcard ACL. |
| `admin` | htpasswd + `admin_acls:admin` with `*:rw` | manual rescue / superuser | The htpasswd rescue path on `iot-staging...:8253/admin/` needs a working superuser. Wildcard ACL. |
| One Google email | OAuth + `admin_acls:<email>` with `*:rw` | end-to-end OAuth testing | Lets the operator sign in via the OAuth flow on `staging.stra2us.austindavid.com` and exercise the full authenticated UI. |

Seeded once at staging bring-up (a `tools/stage seed-users` subcommand,
or baked into `tools/stage up` first-time logic). Idempotent — re-running
should not error if the rows already exist.

### Database state — preserve

Staging Redis state persists across `tools/stage up`/`down`/`rebuild`
cycles by default. Survives container restarts via the bind-mounted
`redis_data_staging/` volume. Resetting is a deliberate operator
action, not the default.

**Future need flagged:** if a schema change ever lands that requires
fresh state, we'll want a `tools/stage nuke` (or `reset-db`) command
that wipes the Redis volume and re-runs the seed step. We don't have
that today — filing as a TODO so it exists when we need it. See
[TODO.md](../TODO.md).

### Synthetic-traffic CLI

Confirmed shape: `stra2us synth-traffic --target HOST:PORT
--client-id staging-probe --duration 5m --rate 2Hz`. Built as a
subcommand of `tools/stra2us_cli`. Tracked as an AI in TODO.md.

### Promote — trust but warn

`tools/stage promote <tag>` does **not** block if `<tag>` wasn't
recently on staging. It prints a clear warning and proceeds:

```
WARNING: tag v1.X.Y has not been verified on staging in this session
  (last staging deploy was: <ref>, deployed <duration> ago).
Continue anyway? [y/N]
```

Operator can override by typing `y` (or pass `--yes` for scripted
promotions). Default is bail. Rationale: aligns with Rule 7 (rollbacks
go to a verified-working target) without being so strict it forces
operator workarounds when the verification was on a previous shell
session.
