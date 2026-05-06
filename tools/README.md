# stra2us-cli

Reference CLI for [Stra2us KV catalogs](../docs/catalog_spec.md).

The stra2us server is a generic signed KV / MQ store. This CLI gives an
app that uses it three things:

1. A hand-written YAML catalog (`<app>.s2s.yaml`) describing the
   tunable variables the app's devices read.
2. A tool (`stra2us`) that reads the catalog to drive
   `catalog / show / set` against the live server, with type / scope /
   range validation drawn from the catalog.
3. A place to point a drift lint from the app repo, so a knob added to
   firmware without a catalog entry fails CI.

The catalog is a **contract with tooling**, not with the device
firmware or the server. See [catalog_spec.md](../docs/catalog_spec.md)
for the full spec and invariants — in particular, never use this CLI
or any tool built on it to write placeholder empty-string values to
"pre-register" a key (it breaks the fallback chain).

---

## Install

From this directory:

```
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

This provides a `stra2us` entry point. `python3 -m stra2us_cli` works
as well.

Requires Python 3.10+.

---

## Credentials

The CLI looks for `host`, `client_id`, and `secret_hex` in this order,
first hit wins **per field**:

1. Explicit flags: `--server`, `--client-id`, `--secret`
2. Environment: `STRA2US_HOST`, `STRA2US_CLIENT_ID`, `STRA2US_SECRET_HEX`
3. `./.stra2us` in the current directory
4. `~/.stra2us` in the user's home directory

### `.stra2us` file format

TOML, with a `[default]` section and optional named profiles:

```toml
[default]
host       = "stra2us.example.com:8153"
client_id  = "dev"
secret_hex = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[profile.prod]
host       = "stra2us.prod.example.com:8153"
client_id  = "ops"
secret_hex = "deadbeef..."
```

Select a named profile with `--profile prod`. Without `--profile`, the
`[default]` section is used.

`host` may be a bare `host:port` (→ `http://`) or a full URL with
scheme. `secret_hex` is the 64-char hex encoding of the client's
32-byte shared secret.

Secrets are sensitive — `chmod 600 ~/.stra2us`.

### Environment fallback

For CI and ad-hoc one-liners, the env vars still work:

```bash
export STRA2US_HOST=stra2us.example.com:8153
export STRA2US_CLIENT_ID=ci-runner
export STRA2US_SECRET_HEX=...
stra2us catalog
```

A `session.sh` wrapper that `export`s these is a reasonable
app-side pattern; the `.stra2us` file is the local-dev alternative.

---

## Commands

All commands auto-detect the catalog file by globbing `*.s2s.yaml` in
the current directory. Override with `--catalog PATH`.

### `stra2us catalog [list]`

Print the variable table from the catalog. No network traffic.
`list` is the default when no catalog subverb is given.

```
$ stra2us catalog
# critterchron — Stra2us catalog (version 1)

  key                 type    scope       default  help
  ----                ----    -----       -------  ----
  heartbeep           int     app,device  300      Stra2us heartbeat cadence in seconds.
  ir                  string  device      —        Script name this device should run.
  ...
```

### `stra2us catalog publish`

Validate the local catalog, then upload its raw YAML text to
`_catalog/<app>` on the server via the existing signed KV endpoint.
Zero new server surface — the catalog lives under the same `/kv/`
namespace every other key does. See
[catalog_spec.md §6](../docs/catalog_spec.md#6--publishing-m2).

```
$ stra2us catalog publish
published: http://stra2us.example.com:8153/kv/_catalog/critterchron (critterchron, 7 vars, 1843 bytes)
```

Validation happens before any network call: malformed catalogs exit
non-zero without touching the server. The client needs `rw` on the
`_catalog` ACL prefix.

### `stra2us catalog fetch [<app>]`

Download the stashed catalog for `<app>` and print the YAML to stdout.
`<app>` is optional; without it, the CLI reads the local catalog's
`app:` field — handy for verifying a publish round-tripped.

```
$ stra2us catalog fetch | diff - critterchron.s2s.yaml && echo ok
ok
```

### `stra2us show <target> [<key>]`

Show the key resolution chain — device scope, app scope, and
compiled-in default — with the effective value and which layer it came
from.

`<target>` is either the literal `app` (app-scope only) or a device
id (device → app → default chain).

```
$ stra2us show ricky heartbeep
heartbeep  (type=int, scope=app,device, default=300)
    device  = 15
    app     = (unset)
    → effective = 15  (from device (ricky))
```

With no `<key>`, prints the resolution chain for every catalog entry.

### `stra2us set <target> <key> <value>`

Write a value after validating type, scope, and range against the
catalog.

```
$ stra2us set ricky heartbeep 15
set: http://stra2us.example.com:8153/kv/critterchron/ricky/heartbeep → 15

$ stra2us set app min_brightness 500
error: min_brightness: value 500 outside catalog range [1, 255]
```

To "unset" a key today, pass `--unset` — the server has no DELETE
verb, so the CLI writes an empty string. Semantics depend on the
variable's type; see the catalog's `help:` text per key.

```
$ stra2us set ricky ir --unset
set: http://.../kv/critterchron/ricky/ir → ''
```

---

## Exit codes

| Code | Meaning                                        |
|------|------------------------------------------------|
| 0    | success                                        |
| 2    | config / credentials / catalog load failure    |
| 3    | validation error (unknown key, wrong scope, range) |
| 4    | server error on write                          |

---

## What lives where

- **In this repo:** the CLI, the catalog schema spec, reference docs.
- **In each app's repo:** the `<app>.s2s.yaml` catalog itself, and the
  drift-lint test that cross-checks catalog entries against the
  firmware's `get_int` / `get_float` call sites. CritterChron's
  `test_s2s_catalog.py` is the current reference implementation.

No codegen. No auto-sync. The catalog is hand-maintained; the drift
lint is the forcing function. See
[invariant 3](../docs/catalog_spec.md#invariant-3--catalog-is-hand-maintained-no-codegen).

---

## Roadmap

- **M1:** spec + CLI verbs `catalog list`, `show`, `set`.
- **M2 (this milestone):** `stra2us catalog publish` / `catalog fetch`.
  YAML text is stashed under `_catalog/<app>` via the existing
  signed `/kv/` endpoint — no new server routes. Optional per-key
  `enforce: true` for advisory server-side range-rejection is still
  deferred.
- **M3:** web UI in the stra2us admin surface that reads published
  catalogs out of `/kv/_catalog/*`.

## Running tests

```
pip install -e .
pip install pytest
pytest tests/
```

The `tests/test_publish_live.py` round-trip test is skipped unless
`STRA2US_HOST` / `STRA2US_CLIENT_ID` / `STRA2US_SECRET_HEX` are set
and point at a reachable server. The rest of the suite is pure-Python.
