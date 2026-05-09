# syntax=docker/dockerfile:1.4
# `# syntax=` directive opts into the modern Dockerfile parser
# regardless of the host Docker version. Required for the
# `--mount=type=cache,...` syntax used below; default on Docker
# ≥ 23.x but the explicit pin makes builds on older daemons
# either succeed cleanly or fail loudly.

FROM python:3.11-slim

# Install system dependencies: redis and supervisor.
#
# `--mount=type=cache,target=/var/cache/apt,sharing=locked
#   --mount=type=cache,target=/var/lib/apt,sharing=locked`
# persists apt's downloaded `.deb` files + package lists on the
# host between builds. Cache mounts are NOT committed to image
# layers (they're build-time-only state), so the image stays
# slim — that's why the prior `rm -rf /var/lib/apt/lists/*`
# becomes unnecessary. `sharing=locked` serializes parallel
# builds touching the same cache so apt's lock file isn't a
# multi-builder hazard.
#
# Filed as a TODO during the v1.6 prod promote — rebuild cycles
# were re-fetching apt + pip on every `--build` despite the
# normal layer cache because invalidations cascaded. See
# docs/csp_admin_audit.md → "Items deferred" for context.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y \
        redis-server \
        supervisor \
        curl

# Optional debug tooling for staging / troubleshooting builds. Default
# off so prod images stay lean and the prod attack surface stays small.
# Set INCLUDE_DEBUG_TOOLS=1 (compose build-arg) to include tcpdump,
# telnet, dig, etc. See docs/staging_environment.md.
ARG INCLUDE_DEBUG_TOOLS=0
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "$INCLUDE_DEBUG_TOOLS" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            tcpdump \
            telnet \
            netcat-openbsd \
            dnsutils \
            iputils-ping \
            procps \
            strace \
            lsof \
            jq \
            vim-tiny ; \
    fi

# Create an isolated venv OUTSIDE /app so it survives the ./backend:/app volume mount
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Build context is the repo root (P5 followup #2 in
# docs/fr_catalog_app_ui_progress.md). Paths in this Dockerfile
# are now repo-rooted: `backend/...` for the application,
# `tools/...` for the CLI package.
WORKDIR /app

# Copy requirements + install backend deps. The pip cache mount
# (`--mount=type=cache,target=/root/.cache/pip`) persists wheel
# downloads between builds — pre-cache-mount, every `--build`
# re-fetched every wheel from PyPI. With the mount, only changed
# pins re-download. The `--no-cache-dir` flag is intentionally
# DROPPED here: it tells pip not to write to its on-disk cache,
# which defeats the cache-mount entirely. The downloaded wheels
# stay in the cache mount (host-persistent, not committed to
# image), so the image stays slim either way.
COPY backend/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Install `stra2us_cli` from the local tools/ tree. NOT editable
# (no `-e`) — the resolved package gets baked into /opt/venv at
# image-build time, so prod's `./backend:/app` runtime mount
# doesn't unmount tools/. To pick up a tools/ change in prod,
# rebuild the image (same as for any non-backend code change).
COPY tools/ /tools/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /tools

# Create necessary directories for volumes and logs
RUN mkdir -p /data /var/log/supervisor /var/run/

# Copy the backend application into /app.
# (Overridden at runtime when the user mounts ./backend:/app)
COPY backend/ .

# Expose the single service port
EXPOSE 8153

# Default environment variables
ENV REDIS_URL=redis://localhost:6379
ENV PYTHONPATH=/app/src

# Start using supervisord to manage both processes
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf"]
