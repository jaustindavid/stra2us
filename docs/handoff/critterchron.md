# CritterChron hand-back — catalog support shipped

Quick note for the CritterChron team on what landed in Stra2us and how
to adopt it. Your original FR lives at
[../catalog_fr.md](../catalog_fr.md), annotated with the decisions we
made while implementing.

## What shipped

**M1 — schema + CLI (in stra2us `tools/`)**
- Canonical schema: [../catalog_spec.md](../catalog_spec.md).
- `stra2us` CLI with `catalog list`, `show`, `set`.
- Strict pydantic validation; drift lint pattern stays app-side (your
  `test_s2s_catalog.py` is the reference implementation).

**M2 — publish / fetch**
- `stra2us catalog publish` — validates the YAML, uploads it to
  `_catalog/{app}` via the existing signed `/kv/` endpoint.
- `stra2us catalog fetch [<app>]` — round-trip check / retrieval.
- No new server routes. Reuses HMAC, ACLs, and the `/kv/` namespace;
  the UI in M3 will read catalogs from `/kv/_catalog/*`.

## Schema deltas from your FR

Two shape changes worth flagging — one affects your YAML, one doesn't:

1. **`default: per-device` → `default_per_device: true`.** The FR used
   a magic string in the typed `default:` field; we split it into its
   own boolean so `default:` stays strictly typed (int / float / bool /
   string / enum member). Your four affected keys — `min_brightness`,
   `max_brightness`, `night_enter_brightness`, `night_exit_brightness`
   — need this flip.
2. **`bool` and `enum` types added.** Didn't exist in your catalog;
   doesn't change anything for you today. Available for future knobs.

The migrated catalog is in this directory as
[critterchron.s2s.yaml](critterchron.s2s.yaml) — drop-in replacement
for the file in your repo root.

## Adopting it

```bash
# One-time install of the CLI
git clone <stra2us-repo>
cd stra2us/tools
python3 -m venv venv && source venv/bin/activate
pip install -e .
```

Credentials via `.stra2us` TOML (see [tools/README.md](../../tools/README.md#credentials))
or env vars. Your existing `ops` client secret works — it just needs
`rw` on the `_catalog` prefix added to its ACL (ops task, one redis
write on the server side).

```bash
# From your repo root (where critterchron.s2s.yaml lives)
stra2us catalog            # sanity-check the list renders
stra2us catalog publish    # upload to _catalog/critterchron
stra2us catalog fetch | diff - critterchron.s2s.yaml   # verify
```

Your drift lint (`test_s2s_catalog.py`) stays as-is — catalog is still
hand-maintained, the device firmware is still the arbiter. Publishing
is additive; not publishing doesn't break anything.

## What's next

M3 is a web UI in the stra2us admin surface that reads catalogs from
`/kv/_catalog/*` and gives ops a point-and-click view of
"which tunables exist, what are they set to, who overrides what."
No firm date; ping when you want to prioritize.
