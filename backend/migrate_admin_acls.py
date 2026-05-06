"""Seed Redis-side ACL records for admin users listed in htpasswd.

Usage:
  python migrate_admin_acls.py                   # grant *:rw to every admin
                                                 # that lacks an ACL row
  python migrate_admin_acls.py --default '*:r'   # different default
  python migrate_admin_acls.py --user alice      # single user
  python migrate_admin_acls.py --force           # overwrite existing rows
  python migrate_admin_acls.py --dry-run         # print plan, write nothing

Idempotent: re-running without --force is a no-op for users that already
have an ACL record. Safe to bake into a post-install / provisioning step.

Reads STRA2US_HTPASSWD (default: ./admin.htpasswd relative to backend/)
and REDIS_URL (default: redis://localhost:6379).
"""

import argparse
import asyncio
import json
import os
import sys

# Make core.* importable when running from backend/ directly.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from core.redis_client import get_redis_client  # noqa: E402
from api.dependencies import ADMIN_ACL_KEY_FMT   # noqa: E402


DEFAULT_HTPASSWD = os.environ.get(
    "STRA2US_HTPASSWD",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.htpasswd"),
)


def read_htpasswd_users(path: str) -> list[str]:
    if not os.path.exists(path):
        raise SystemExit(f"htpasswd not found: {path}")
    users = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            users.append(line.split(":", 1)[0])
    return users


def parse_default(spec: str) -> dict:
    """'prefix:access' -> {'prefix': ..., 'access': ...}. Rejects
    malformed input early so the tool doesn't write garbage."""
    if ":" not in spec:
        raise SystemExit(f"--default must be 'prefix:access', got {spec!r}")
    prefix, access = spec.rsplit(":", 1)
    if access not in ("r", "rw"):
        raise SystemExit(f"--default access must be 'r' or 'rw', got {access!r}")
    if not prefix:
        raise SystemExit("--default prefix must be non-empty (use '*' for wildcard)")
    return {"prefix": prefix, "access": access}


async def run(htpasswd: str, default: dict, user_filter: str | None, force: bool, dry_run: bool) -> int:
    users = read_htpasswd_users(htpasswd)
    if user_filter:
        if user_filter not in users:
            raise SystemExit(f"user {user_filter!r} not found in {htpasswd}")
        users = [user_filter]

    redis = get_redis_client()
    summary = {"created": [], "skipped_existing": [], "overwritten": []}

    for user in users:
        key = ADMIN_ACL_KEY_FMT.format(user=user)
        existing = await redis.get(key)

        if existing and not force:
            summary["skipped_existing"].append(user)
            continue

        acl_envelope = {"permissions": [default]}
        if dry_run:
            action = "would overwrite" if existing else "would create"
            print(f"  [dry-run] {action} {key} -> {json.dumps(acl_envelope)}")
        else:
            await redis.set(key, json.dumps(acl_envelope))

        if existing:
            summary["overwritten"].append(user)
        else:
            summary["created"].append(user)

    print()
    print(f"htpasswd:         {htpasswd}")
    print(f"default ACL:      {default['prefix']}:{default['access']}")
    print(f"users considered: {len(users)}")
    print(f"  created:           {len(summary['created'])}  {summary['created']}")
    print(f"  overwritten:       {len(summary['overwritten'])}  {summary['overwritten']}")
    print(f"  skipped (existed): {len(summary['skipped_existing'])}  {summary['skipped_existing']}")
    if dry_run:
        print("(dry-run — no changes written)")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--htpasswd", default=DEFAULT_HTPASSWD,
                   help=f"path to htpasswd file (default: {DEFAULT_HTPASSWD})")
    p.add_argument("--default", dest="default_spec", default="*:rw",
                   help="default ACL as 'prefix:access' for users with no record (default: *:rw)")
    p.add_argument("--user", default=None, help="only migrate this one user")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing ACL records (destructive)")
    p.add_argument("--dry-run", action="store_true",
                   help="print plan without writing")
    args = p.parse_args()

    default = parse_default(args.default_spec)
    return asyncio.run(run(args.htpasswd, default, args.user, args.force, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
