# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Per-device value resolution for the customer-facing app form
(P3 of `docs/fr_catalog_app_ui_plan.md`).

The customer page wants to display the *current* value of each
catalog variable for a specific device. The resolution order
follows the existing `<app>/<device>/<key>` → `<app>/public/<key>`
fallback chain documented in
`docs/fr_application_view.md`:

1. Per-device override at `kv:<app>/<device>/<key>`.
2. App-scope default at `kv:<app>/public/<key>`.
3. Catalog's compiled-in `default:` value.
4. None — caller renders the input with `value=""`.

Encrypted-flag handling stays out of scope for P3: the page emits
a placeholder + Reveal button (existing app.js path) for any var
whose `kv:<key>:enc` flag is set. Server-side decryption per
admin auth is the existing pattern; this module signals
"encrypted" by returning a sentinel and lets the renderer pick
the masked-display path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import msgpack


@dataclass
class ResolvedValue:
    """The customer-facing renderer's view of one var.

    `value` is the resolved current value (post-fallback chain) as
    a string suitable for HTML form inputs, or None when the chain
    bottoms out without a value. `encrypted` flags secret records
    whose plaintext should not enter the page HTML — the renderer
    emits an empty input + Reveal trigger instead. `from_default`
    is True when the value came from the catalog's `default:`
    field (no KV record), letting the renderer hint "still using
    the catalog default" in the UI."""
    value: str | None
    encrypted: bool = False
    from_default: bool = False


async def _read_kv(redis, key: str) -> tuple[Any | None, bool]:
    """Read `kv:<key>` and return `(decoded_value, encrypted_flag)`.

    Returns `(None, False)` when the key is absent. Decodes the
    msgpack-wrapped value (matching what the CLI publishes).
    """
    raw = await redis.get(f"kv:{key}")
    if raw is None:
        return None, False
    encrypted = bool(await redis.get(f"kv:{key}:enc"))
    try:
        decoded = msgpack.unpackb(raw, raw=False)
    except Exception:
        # Corrupted record — surface as "no value" rather than
        # 500ing the page render. The operator can re-publish.
        return None, encrypted
    return decoded, encrypted


async def resolve_value(redis, app: str, device: str,
                        var_name: str, var: dict) -> ResolvedValue:
    """Walk the resolution chain for one catalog var.

    The catalog's `default:` is consulted only after both KV
    locations miss — matches the pre-P3 customer page's behavior
    (and the device firmware's read order, per
    `docs/fr_application_view.md`).
    """
    # 1. Per-device override.
    value, encrypted = await _read_kv(redis, f"{app}/{device}/{var_name}")
    if value is not None:
        return ResolvedValue(_to_form_string(value), encrypted=encrypted)

    # 2. App-scope default.
    value, encrypted = await _read_kv(redis, f"{app}/public/{var_name}")
    if value is not None:
        return ResolvedValue(_to_form_string(value), encrypted=encrypted)

    # 3. Catalog's compiled-in default.
    default = var.get("default")
    if default is not None:
        return ResolvedValue(_to_form_string(default), from_default=True)

    # 4. Genuinely unset.
    return ResolvedValue(value=None)


def _to_form_string(value: Any) -> str:
    """Coerce a Python value to the string shape an HTML form
    input expects.

    * bool → `"true"` / `"false"` (CLI's `coerce_value` accepts
      either case; we emit lowercase).
    * int / float → `str(value)` (rounds to repr — no precision
      loss for typical catalog values).
    * str → as-is.
    * Anything else → `str(value)` as a last resort.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return str(value)
