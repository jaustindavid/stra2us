#!/usr/bin/env bash
# tools/sync-secrets.sh — push host-bound credentials from this dev
# checkout to the deploy host. Run on DEV, not on host.
#
# Source files (on dev, gitignored, never sourced by your local shell):
#   .env.host-prod     →  pushed to $PROD_DIR/.env on host
#   .env.host-staging  →  pushed to $STAGING_DIR/.env.staging on host
#
# These are kept SEPARATE from the dev-side .env / .env.staging files
# (which are for running tests locally against deployed environments).
# Cleaner separation: prod secrets never end up in your local shell
# environment by accident.
#
# Contents expected:
#   .env.host-prod      CLOUDFLARE_TUNNEL_TOKEN, STRA2US_GOOGLE_CLIENT_ID,
#                       STRA2US_GOOGLE_CLIENT_SECRET (prod values)
#   .env.host-staging   same vars, staging values + SMOKE_ADMIN_PASS for
#                       the staging smoke-test seed
#
# Usage:
#   tools/sync-secrets.sh             # sync both files
#   tools/sync-secrets.sh --dry-run   # show what would happen, no copy

set -x
set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$REPO_ROOT/tools/.deploy-config"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: $CONFIG_FILE not found." >&2
    echo "Copy tools/.deploy-config.example to tools/.deploy-config and fill in values." >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

for v in DEPLOY_HOST DEPLOY_USER PROD_DIR STAGING_DIR; do
    if [[ -z "${!v:-}" ]]; then
        echo "ERROR: $v is empty in $CONFIG_FILE" >&2
        exit 1
    fi
done
PORT="${DEPLOY_PORT:-22}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
    echo "(dry run — no files will be copied)"
    echo
fi

ENV_PROD="$REPO_ROOT/.env.host-prod"
ENV_STAGING="$REPO_ROOT/.env.host-staging"

if [[ ! -f "$ENV_PROD" && ! -f "$ENV_STAGING" ]]; then
    echo "ERROR: neither .env.host-prod nor .env.host-staging exists in $REPO_ROOT" >&2
    echo "These are dev-side source files for the host's .env / .env.staging." >&2
    echo "See tools/sync-secrets.sh header for expected contents." >&2
    exit 1
fi

# --- helpers ---------------------------------------------------------

push_one() {
    local src="$1" dst_path="$2" label="$3"
    if [[ ! -f "$src" ]]; then
        printf "  [skip]  %s — not present locally\n" "$label"
        return
    fi
    local target="$DEPLOY_USER@$DEPLOY_HOST:$dst_path"
    printf "  [push]  %s → %s\n" "$label" "$target"
    if (( DRY_RUN )); then
        return
    fi
    # scp over ssh. We tried rsync; on Synology, rsync auth fails
    # while scp succeeds — Synology's sshd allows the scp/sftp
    # subsystem for users that don't have a full interactive-shell
    # privilege, and rsync requires the latter. -p preserves mtime
    # so re-runs only push when local file changes (cheap dedup);
    # BatchMode=yes fails fast on auth prompts.
    scp -p -o BatchMode=yes -P "$PORT" "$src" "$target"
}

# Verify SSH connectivity before trying to push anything. Catches
# bad host/user/port settings with a clear error.
echo "→ Testing SSH to $DEPLOY_USER@$DEPLOY_HOST (port $PORT)..."
if (( DRY_RUN )); then
    echo "  (dry run, skipping connectivity test)"
else
    if ! ssh -p "$PORT" -o BatchMode=yes -o ConnectTimeout=10 \
              "$DEPLOY_USER@$DEPLOY_HOST" "echo ok" >/dev/null 2>&1; then
        echo "ERROR: SSH connection failed." >&2
        echo "  Verify DEPLOY_HOST/DEPLOY_USER/DEPLOY_PORT in $CONFIG_FILE" >&2
        echo "  and that key-based auth is set up (BatchMode=yes disables prompts)." >&2
        exit 1
    fi
    echo "  OK"
fi

echo
echo "→ Syncing secrets:"
push_one "$ENV_PROD"    "$PROD_DIR/.env"            ".env.host-prod    → host .env"
push_one "$ENV_STAGING" "$STAGING_DIR/.env.staging" ".env.host-staging → host .env.staging"

echo
echo "✓ Done."
if (( DRY_RUN )); then
    echo "  (this was a dry run — no files actually copied)"
fi
