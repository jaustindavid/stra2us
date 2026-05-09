FROM python:3.11-slim

# Install system dependencies: redis and supervisor
RUN apt-get update && apt-get install -y \
    redis-server \
    supervisor \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Optional debug tooling for staging / troubleshooting builds. Default
# off so prod images stay lean and the prod attack surface stays small.
# Set INCLUDE_DEBUG_TOOLS=1 (compose build-arg) to include tcpdump,
# telnet, dig, etc. See docs/staging_environment.md.
ARG INCLUDE_DEBUG_TOOLS=0
RUN if [ "$INCLUDE_DEBUG_TOOLS" = "1" ]; then \
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
            vim-tiny \
        && rm -rf /var/lib/apt/lists/* ; \
    fi

# Create an isolated venv OUTSIDE /app so it survives the ./backend:/app volume mount
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Build context is the repo root (P5 followup #2 in
# docs/fr_catalog_app_ui_progress.md). Paths in this Dockerfile
# are now repo-rooted: `backend/...` for the application,
# `tools/...` for the CLI package.
WORKDIR /app

# Copy requirements + install backend deps
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install `stra2us_cli` from the local tools/ tree. NOT editable
# (no `-e`) — the resolved package gets baked into /opt/venv at
# image-build time, so prod's `./backend:/app` runtime mount
# doesn't unmount tools/. To pick up a tools/ change in prod,
# rebuild the image (same as for any non-backend code change).
COPY tools/ /tools/
RUN pip install --no-cache-dir /tools

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
