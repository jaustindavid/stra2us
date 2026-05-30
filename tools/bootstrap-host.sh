#!/usr/bin/env bash
# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
# tools/bootstrap-host.sh — set up the prod + staging directory layout
# on the deploy host. Run ONCE on the host (not from dev).
#
# Idempotent: re-running on an already-bootstrapped host is a no-op.
#
# Prereqs:
#   - tools/.deploy-config exists and is filled in
#   - git on the host is authenticated to the configured GIT_REMOTE
#   - docker compose is available without sudo
#
# What this does:
#   - Creates PROD_DIR and STAGING_DIR (parent dirs included).
#   - git clones GIT_REMOTE into each (skips if already a repo).
#   - Creates the host-side volume directories under each clone.
#   - Seeds admin.htpasswd from admin.htpasswd.default (merge, not
#     overwrite — see seed_htpasswd below).
#   - Prints next-steps guidance for syncing secrets and seeding users.

set -eu

# --- function definitions ---------------------------------------------
# Defined unconditionally so tests/tools can source this file and
# exercise these functions in isolation. The "do the bootstrap" body
# at the bottom of the file is gated on "invoked as a script" so
# sourcing is safe.

clone_or_skip() {
    local target="$1"
    if [[ -d "$target/.git" ]]; then
        echo "→ $target is already a git repo; skipping clone"
    elif [[ -e "$target" ]]; then
        echo "ERROR: $target exists but is not a git repo. Refusing to overwrite." >&2
        exit 1
    else
        echo "→ cloning into $target"
        mkdir -p "$(dirname "$target")"
        git clone "$GIT_REMOTE" "$target"
    fi
}

ensure_dir() {
    local d="$1"
    if [[ ! -d "$d" ]]; then
        echo "→ mkdir $d"
        mkdir -p "$d"
    fi
}

# Seed `backend/admin.htpasswd` from `backend/admin.htpasswd.default`.
# MERGE behavior, not overwrite:
#   - If default file is absent: nothing to do.
#   - If live file is absent: copy default in full.
#   - If live file exists: for each line in default, append to live
#     ONLY if that username is not already in live. Existing entries
#     are never overwritten — operator's password is sacred.
#
# Idempotent: running multiple times never duplicates a user, never
# clobbers an operator-set password.
seed_htpasswd() {
    local target="$1"
    local default_file="$target/backend/admin.htpasswd.default"
    local live_file="$target/backend/admin.htpasswd"

    if [[ ! -f "$default_file" ]]; then
        echo "→ $default_file not found — skipping (manually create htpasswd later)"
        return
    fi

    if [[ ! -f "$live_file" ]]; then
        echo "→ $live_file not present, seeding from admin.htpasswd.default"
        cp "$default_file" "$live_file"
        echo "  ⚠️  The seeded 'rescue' user has a PLACEHOLDER password (a hash"
        echo "      of a random string nobody knows — unusable as-is) but maps to"
        echo "      FULL SUPERUSER. Before exposing this host, set a strong one:"
        echo "        cd backend && python3 create_admin.py rescue \"\$(openssl rand -base64 24)\""
        echo "      (or delete the rescue line and rely on OAuth). See"
        echo "      backend/admin.htpasswd.default for the full rationale."
        return
    fi

    # Merge — append-if-missing per username.
    local added=0 skipped=0 line username
    while IFS= read -r line; do
        # Skip blank lines and operator-added comments. The htpasswd
        # parser (admin_auth.py) ignores anything that doesn't have a
        # `:`, so being permissive here matches that behavior.
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        username="${line%%:*}"
        if grep -q "^${username}:" "$live_file" 2>/dev/null; then
            skipped=$((skipped + 1))
        else
            printf '%s\n' "$line" >> "$live_file"
            added=$((added + 1))
            echo "  → added '$username' from default"
        fi
    done < "$default_file"

    if (( added == 0 )); then
        echo "→ $live_file: all default users already present (skipped $skipped)"
    else
        echo "→ $live_file: added $added user(s), skipped $skipped already-present"
    fi
}

# --- main bootstrap (only when invoked as a script, not sourced) ------

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
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

    # Required vars
    for v in PROD_DIR STAGING_DIR GIT_REMOTE; do
        if [[ -z "${!v:-}" ]]; then
            echo "ERROR: $v is empty in $CONFIG_FILE" >&2
            exit 1
        fi
    done

    echo
    echo "About to bootstrap stra2us deploy directories:"
    echo
    echo "  Prod clone:    $PROD_DIR"
    echo "  Staging clone: $STAGING_DIR"
    echo "  Git remote:    $GIT_REMOTE"
    echo
    read -rp "Looks right? [y/N] " confirm
    case "$confirm" in
        y|Y) ;;
        *) echo "aborted"; exit 1 ;;
    esac

    clone_or_skip "$PROD_DIR"
    clone_or_skip "$STAGING_DIR"

    # Volume dirs on the host. These get bind-mounted into containers per
    # the compose files. Created here so first `docker compose up` doesn't
    # accidentally create them as root-owned by docker daemon.
    ensure_dir "$PROD_DIR/redis_data"
    ensure_dir "$STAGING_DIR/redis_data_staging"

    seed_htpasswd "$PROD_DIR"
    seed_htpasswd "$STAGING_DIR"

    cat <<EOF

✓ Bootstrap complete.

Next steps:

1. From your DEV machine, push the host-bound secrets:

     tools/sync-secrets.sh

   Source files on dev (gitignore them, never source from your shell):
     .env.host-prod     → host's $PROD_DIR/.env
     .env.host-staging  → host's $STAGING_DIR/.env.staging

   See the sync-secrets.sh header for expected contents of each.
   The dev-side .env / .env.staging stay minimal (smoke-test creds
   for testing deployed environments locally).

2. On the HOST, in $PROD_DIR:

     cd $PROD_DIR
     # one-time: provision the prod admin htpasswd entry
     cd backend && python3 create_admin.py admin '<chosen-password>' && cd ..
     # ⚠️  MANDATORY: rotate the placeholder 'rescue' password (it maps to
     #     full superuser; shipped value is an unusable random-hash). Use a
     #     STRONG random one and save it in your password manager:
     cd backend && python3 create_admin.py rescue "\$(openssl rand -base64 24)" && cd ..
     #     (or delete the rescue line and rely solely on OAuth)
     # bring up prod
     docker compose up -d
     # wait for tunnel + smoke (manual today; deploy.sh wraps later)
     # The admin dashboard shows a rescue-on-default banner until rotated.

3. On the HOST, in $STAGING_DIR:

     cd $STAGING_DIR
     tools/stage up
     tools/stage wait-tunnel
     tools/stage seed-users
     tools/stage smoke

EOF
fi
