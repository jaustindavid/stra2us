# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Command-line interface.

Verbs:

    stra2us catalog [list]                  Print the variable table. No network.
    stra2us catalog publish                 Upload the catalog YAML to the server
                                            at _catalog/{app}. Validates first.
    stra2us catalog fetch [<app>]           Download the stashed catalog for <app>
                                            (defaults to the local catalog's app).

    stra2us show <target> [<key>]           Show resolution chain for a key.
                                            <target> = "app" or a device id.

    stra2us set <target> <key> <value>      Write a catalog-declared key.
    stra2us set <target> <key> --unset      Write empty string (no DELETE yet).

Global flags:
    --catalog PATH        Explicit catalog file. Default: glob *.s2s.yaml in CWD.
    --profile NAME        Select profile from .stra2us.
    --server / --client-id / --secret
                          Override config + env (see config.py).

Reserved target names: "app". Any other string is treated as a device id.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .catalog import Catalog, CatalogError, Var, coerce_value, kv_path, load_catalog
from .catalog_lint import errors as lint_errors, format_issues, lint_catalog, warnings as lint_warnings
from .catalog_publish import (
    PublishError,
    discover_assets,
    lint_loaded,
    publish_assets,
)
from .client import Stra2usClient, Stra2usError
from .config import ConfigError, resolve

APP_SCOPE_KEYWORD = "app"
CATALOG_STASH_PREFIX = "_catalog"


# ----- helpers -----

def _autodetect_catalog(cwd: Path) -> Path:
    matches = sorted(cwd.glob("*.s2s.yaml"))
    if not matches:
        raise CatalogError(
            f"no *.s2s.yaml in {cwd}; pass --catalog PATH"
        )
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise CatalogError(
            f"multiple catalogs in {cwd} ({names}); pass --catalog PATH"
        )
    return matches[0]


def _catalog_path(args: argparse.Namespace) -> Path:
    return Path(args.catalog) if args.catalog else _autodetect_catalog(Path.cwd())


def _load(args: argparse.Namespace) -> Catalog:
    return load_catalog(_catalog_path(args))


def _build_client(args: argparse.Namespace) -> Stra2usClient:
    creds = resolve(
        server=args.server,
        client_id=args.client_id,
        secret_hex=args.secret_hex,
        profile=args.profile,
    )
    return Stra2usClient(
        base_url=creds.base_url,
        client_id=creds.client_id,
        secret_hex=creds.secret_hex,
    )


def _parse_target(target: str) -> tuple[str, str | None]:
    """Return (scope, device). scope ∈ {"app", "device"}; device is None at app scope."""
    if target == APP_SCOPE_KEYWORD:
        return ("app", None)
    return ("device", target)


def _validate_scope(var: Var, scope: str, key: str) -> None:
    if scope not in var.scope:
        raise CatalogError(
            f"{key!r} is not valid at {scope} scope "
            f"(catalog scope={list(var.scope)})"
        )


def _fmt_default(var: Var) -> str:
    if var.default_per_device:
        return "per-device"
    if var.default is None:
        return "—"
    return repr(var.default)


def _stash_key(app: str) -> str:
    return f"{CATALOG_STASH_PREFIX}/{app}"


# ----- catalog group verbs -----

def cmd_catalog_list(args: argparse.Namespace) -> int:
    cat = _load(args)
    print(f"# {cat.app} — Stra2us catalog (version {cat.version})\n")

    rows = []
    for name, v in cat.vars.items():
        rows.append((
            name,
            v.type,
            ",".join(v.scope),
            _fmt_default(v),
            (v.help or "").strip().splitlines()[0] if v.help else "",
        ))
    headers = ("key", "type", "scope", "default", "help")
    widths = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths[:-1]) + "  {}"
    print(fmt.format(*headers))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt.format(*r))
    return 0


def cmd_catalog_publish(args: argparse.Namespace) -> int:
    """Validate the local catalog, run lint, sanitize and upload any
    sibling `_assets/`, then commit the catalog YAML — in the order
    the FR specifies (assets first, catalog YAML as the commit point,
    `_assets_index` updated, dropped files GC'd last). See
    `docs/fr_catalog_app_ui.md` "Implementation outline" §5a +
    `docs/fr_catalog_app_ui_plan.md` P1.

    Validation passes (in order; each is a publish-blocking gate):
      1. `load_catalog` — strict pydantic schema.
      2. `discover_assets` — walks `_assets/`; SVGs go through the P0
         sanitizer; rejected SVGs raise `PublishError`.
      3. `lint_catalog` + `lint_asset_bundle` — semantic + UI-hint
         constraints from the FR's lint table, plus per-bundle
         size/type/filename limits.

    Exit codes:
      0  — published.
      2  — config error (missing creds).
      4  — network / signing error from the server.
      5  — catalog lint failure (your YAML is wrong).
      6  — asset-pipeline failure (bad SVG, bundle limit, etc.).
    """
    path = _catalog_path(args)
    cat = load_catalog(path)

    # --- assets pass ---
    # Sanitize + hash *before* lint so the size-after-sanitize is what
    # the bundle-size cap evaluates. A vendor's pre-sanitize SVG might
    # exceed the cap; the cleaned tree is what hits KV, so the cleaned
    # size is the relevant figure.
    asset_dir = path.parent / "_assets"
    try:
        loaded = discover_assets(asset_dir) if asset_dir.is_dir() else []
    except PublishError as e:
        print(f"error: {e}", file=sys.stderr)
        return 6

    # `asset_listing=None` would tell lint "no asset context" and skip
    # the existence check on `theme.logo_asset`. We have an authoritative
    # listing from the loaded bundle (post-discovery, post-skip-of-
    # subdirs), so pass it explicitly — even when empty.
    asset_listing = {a.filename for a in loaded} if asset_dir.is_dir() else None

    # --- lint passes (catalog + bundle) ---
    issues = lint_catalog(cat, asset_listing=asset_listing)
    issues.extend(lint_loaded(loaded))
    errs = lint_errors(issues)
    warns = lint_warnings(issues)
    for w in warns:
        print(f"warning: {w.path}: {w.message}", file=sys.stderr)
    if errs:
        print("error: catalog lint failed:", file=sys.stderr)
        for e in errs:
            print(f"  {e.path}: {e.message}", file=sys.stderr)
        return 5

    yaml_text = path.read_text()

    try:
        client = _build_client(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Directory presence is the opt-in for asset management. A
    # catalog without an `_assets/` directory falls back to the
    # pre-P1 publish path (just the catalog YAML) — does NOT touch
    # whatever asset bundle the previous publish stashed. Users
    # who want to clear assets create an empty `_assets/` directory
    # and republish; that's the explicit signal.
    try:
        if asset_dir.is_dir():
            result = publish_assets(client, cat.app, loaded, yaml_text)
        else:
            client.put(f"_catalog/{cat.app}", yaml_text)
            result = {"assets_uploaded": 0, "assets_dropped": 0,
                      "dropped_filenames": [],
                      "assets_unmanaged": True}
    except PublishError as e:
        print(f"error: {e}", file=sys.stderr)
        return 6
    except Stra2usError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4

    size = len(yaml_text.encode("utf-8"))
    msg = (
        f"published: {client.base_url}/kv/_catalog/{cat.app} "
        f"({cat.app}, {len(cat.vars)} vars, {size} bytes"
    )
    if result["assets_uploaded"]:
        msg += f", {result['assets_uploaded']} assets"
    if result["assets_dropped"]:
        msg += f", GC'd {result['assets_dropped']} dropped"
    if result.get("assets_unmanaged"):
        msg += ", assets unmanaged"
    msg += ")"
    print(msg)
    return 0


def cmd_catalog_fetch(args: argparse.Namespace) -> int:
    """Download the stashed catalog for <app>. Prints YAML to stdout.

    <app> is optional; falls back to the local catalog's `app:` field
    when a catalog file is available."""
    app = args.app
    if app is None:
        try:
            cat = _load(args)
        except CatalogError as e:
            print(
                f"error: no <app> given and local catalog unavailable: {e}",
                file=sys.stderr,
            )
            return 2
        app = cat.app

    try:
        client = _build_client(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    stash_key = _stash_key(app)
    try:
        value = client.get(stash_key)
    except Stra2usError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4

    if value is None:
        print(
            f"error: no catalog stashed for {app!r} at {stash_key}",
            file=sys.stderr,
        )
        return 3
    if not isinstance(value, str):
        print(
            f"error: stashed value at {stash_key} is not a string "
            f"(got {type(value).__name__})",
            file=sys.stderr,
        )
        return 4

    sys.stdout.write(value)
    if not value.endswith("\n"):
        sys.stdout.write("\n")
    return 0


# ----- show / set -----

def _fetch_scope(client: Stra2usClient, full_key: str) -> tuple[str, object]:
    """Returns ("set", value) | ("unset", None) | ("error", msg)."""
    try:
        v = client.get(full_key)
    except Stra2usError as e:
        return ("error", str(e))
    if v is None or v == "":
        return ("unset", None)
    return ("set", v)


def _fmt_cell(state: str, value: object) -> str:
    if state == "n/a":
        return "(not valid at this scope)"
    if state == "unset":
        return "(unset)"
    if state == "error":
        return f"(error: {value})"
    return repr(value)


def cmd_show(args: argparse.Namespace) -> int:
    cat = _load(args)
    scope, device = _parse_target(args.target)

    if args.key and args.key not in cat.vars:
        print(
            f"error: {args.key!r} not in catalog. Keys: "
            f"{', '.join(cat.vars)}",
            file=sys.stderr,
        )
        return 3

    try:
        client = _build_client(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    keys = [args.key] if args.key else list(cat.vars)
    for k in keys:
        var = cat.vars[k]
        scopes = list(var.scope)
        print(
            f"{k}  (type={var.type}, scope={','.join(scopes)}, "
            f"default={_fmt_default(var)})"
        )

        dev_state: str = "n/a"
        dev_val: object = None
        app_state: str = "n/a"
        app_val: object = None

        if scope == "device" and "device" in scopes:
            dev_state, dev_val = _fetch_scope(
                client, kv_path(cat.app, k, device)
            )
        if "app" in scopes:
            app_state, app_val = _fetch_scope(client, kv_path(cat.app, k, None))

        # Resolution: device (if set) → app (if set) → default.
        if dev_state == "set":
            effective: object = dev_val
            source = f"device ({device})"
        elif app_state == "set":
            effective = app_val
            source = "app"
        elif var.default_per_device:
            effective = "(per-device default)"
            source = "firmware"
        elif var.default is not None:
            effective = var.default
            source = "catalog default"
        else:
            effective = "(undefined)"
            source = "no default declared"

        if scope == "device":
            print(f"    device  = {_fmt_cell(dev_state, dev_val)}")
        print(f"    app     = {_fmt_cell(app_state, app_val)}")
        print(f"    → effective = {effective!r}  (from {source})")
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    """Raw KV write — bypasses catalog validation. For binary blobs and
    keys not modeled in the catalog (e.g. compiled IR scripts written to
    `<app>/scripts/<name>`). Use `set` instead for catalog-declared keys
    so type/range/scope are checked."""
    if (args.file is None) == (args.value is None):
        print("error: pass exactly one of --file or --value", file=sys.stderr)
        return 2

    if args.file is not None:
        try:
            with open(args.file, "rb") as f:
                value: object = f.read()
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    else:
        value = args.value

    try:
        client = _build_client(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        client.put(args.key, value, encrypted=args.encrypted)
    except Stra2usError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4

    size = len(value) if isinstance(value, (bytes, str)) else "?"
    kind = "bytes" if isinstance(value, bytes) else "string"
    enc = " [encrypted]" if args.encrypted else ""
    print(f"put: {client.base_url}/kv/{args.key} ({size} {kind}){enc}")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    """Raw KV read — bypasses catalog. Writes the value to stdout (bytes
    binary-safe via stdout.buffer, strings as utf-8, anything else as
    JSON). Exits 1 with no output when the key is unset, so a script can
    branch on the exit code."""
    try:
        client = _build_client(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        value = client.get(args.key)
    except Stra2usError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4

    if value is None:
        return 1

    if isinstance(value, bytes):
        out_bytes = value
    elif isinstance(value, str):
        out_bytes = value.encode("utf-8")
    else:
        import json
        out_bytes = json.dumps(value).encode("utf-8")

    if args.output is not None:
        try:
            with open(args.output, "wb") as f:
                f.write(out_bytes)
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    else:
        sys.stdout.buffer.write(out_bytes)
        sys.stdout.flush()

    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Raw KV delete — bypasses catalog. Idempotent."""
    try:
        client = _build_client(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        client.delete(args.key)
    except Stra2usError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4

    print(f"del: {client.base_url}/kv/{args.key}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    cat = _load(args)
    scope, device = _parse_target(args.target)

    if args.key not in cat.vars:
        print(
            f"error: {args.key!r} not in catalog. Add an entry to the "
            f"catalog YAML first.",
            file=sys.stderr,
        )
        return 3
    var = cat.vars[args.key]

    try:
        _validate_scope(var, scope, args.key)
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    if args.unset:
        value: object = ""
    else:
        if args.value is None:
            print("error: value required (or pass --unset)", file=sys.stderr)
            return 3
        try:
            value = coerce_value(var, args.value, args.key)
        except CatalogError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3

    full_key = kv_path(cat.app, args.key, device)

    try:
        client = _build_client(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        client.put(full_key, value, encrypted=args.encrypted)
    except Stra2usError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4

    enc = " [encrypted]" if args.encrypted else ""
    print(f"set: {client.base_url}/kv/{full_key} → {value!r}{enc}")
    return 0


# ----- entrypoint -----

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stra2us",
        description="Reference CLI for Stra2us KV catalogs.",
    )
    p.add_argument("--catalog", help="path to <app>.s2s.yaml (default: glob *.s2s.yaml in CWD)")
    p.add_argument("--profile", help="profile name from .stra2us")
    p.add_argument("--server", help="stra2us host URL (overrides env + .stra2us)")
    p.add_argument("--client-id", dest="client_id")
    p.add_argument("--secret", dest="secret_hex")

    sub = p.add_subparsers(dest="verb", required=True)

    # ----- catalog group -----
    sp_cat = sub.add_parser(
        "catalog", help="catalog operations (list/publish/fetch)"
    )
    cat_sub = sp_cat.add_subparsers(dest="catalog_verb", required=False)
    cat_sub.add_parser("list", help="print the catalog table (default)")
    cat_sub.add_parser(
        "publish",
        help=f"upload catalog to {CATALOG_STASH_PREFIX}/<app> on the server",
    )
    sp_fetch = cat_sub.add_parser(
        "fetch",
        help=f"download stashed catalog from {CATALOG_STASH_PREFIX}/<app>",
    )
    sp_fetch.add_argument(
        "app",
        nargs="?",
        help="app name (default: from local catalog's `app:` field)",
    )

    # ----- show -----
    sp_show = sub.add_parser(
        "show", help="show resolution chain for a device or app scope"
    )
    sp_show.add_argument(
        "target",
        help=f"'{APP_SCOPE_KEYWORD}' for app scope, or a device id",
    )
    sp_show.add_argument("key", nargs="?", help="catalog key (omit for all)")

    # ----- put -----
    sp_put = sub.add_parser(
        "put",
        help="write a raw KV value (bytes from a file or inline string); bypasses catalog validation",
    )
    sp_put.add_argument(
        "key",
        help="full KV path, e.g. critterchron/scripts/thyme",
    )
    put_input = sp_put.add_mutually_exclusive_group(required=True)
    put_input.add_argument(
        "--file",
        help="read raw bytes from this path; written as msgpack bin",
    )
    put_input.add_argument(
        "--value",
        help="inline string value; written as msgpack str",
    )
    sp_put.add_argument(
        "--encrypted",
        action="store_true",
        help="mark this record encrypted on the server; subsequent GETs ship as ciphertext (see docs/fr_encrypted_values.md)",
    )

    # ----- get -----
    sp_get = sub.add_parser(
        "get",
        help="read a raw KV value (bytes/str/json) to stdout; exits 1 if unset",
    )
    sp_get.add_argument("key", help="full KV path")
    sp_get.add_argument(
        "--output", "-o",
        help="write to this file instead of stdout (recommended for binary)",
    )

    # ----- del -----
    sp_del = sub.add_parser(
        "del",
        help="delete a KV entry; idempotent (no error if key didn't exist)",
    )
    sp_del.add_argument("key", help="full KV path")

    # ----- set -----
    sp_set = sub.add_parser(
        "set", help="write a catalog-declared key to KV"
    )
    sp_set.add_argument("target", help=f"'{APP_SCOPE_KEYWORD}' or device id")
    sp_set.add_argument("key")
    sp_set.add_argument("value", nargs="?")
    sp_set.add_argument(
        "--unset",
        action="store_true",
        help="write empty string (server has no DELETE verb)",
    )
    sp_set.add_argument(
        "--encrypted",
        action="store_true",
        help="mark this record encrypted on the server; subsequent GETs ship as ciphertext (see docs/fr_encrypted_values.md)",
    )
    return p


def _dispatch_catalog(args: argparse.Namespace) -> int:
    verb = args.catalog_verb or "list"
    if verb == "list":
        return cmd_catalog_list(args)
    if verb == "publish":
        return cmd_catalog_publish(args)
    if verb == "fetch":
        return cmd_catalog_fetch(args)
    # argparse rejects unknown subcommands; this is defensive.
    print(f"error: unknown catalog verb {verb!r}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.verb == "catalog":
            return _dispatch_catalog(args)
        if args.verb == "show":
            return cmd_show(args)
        if args.verb == "put":
            return cmd_put(args)
        if args.verb == "get":
            return cmd_get(args)
        if args.verb == "del":
            return cmd_delete(args)
        if args.verb == "set":
            return cmd_set(args)
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    parser.error(f"unknown verb {args.verb!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
