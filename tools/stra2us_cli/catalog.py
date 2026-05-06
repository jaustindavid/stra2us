"""Catalog schema + loader.

See docs/catalog_spec.md for the canonical spec. This module is a
strict pydantic mirror of that spec, plus `coerce_value` which the CLI
uses to parse operator input against a variable's declared type.

Keep the schema tight: the point of having a schema at all is to catch
typos and drift at load time, not to discover them when a write fails
on the wire.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


# ----- schema -----

VAR_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

Scope = Literal["app", "device"]
VarType = Literal["int", "float", "string", "bool", "enum"]


class Var(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: VarType
    scope: list[Scope] = Field(min_length=1)
    default: int | float | str | bool | None = None
    default_per_device: bool = False
    default_per_platform: bool = False
    range: tuple[int | float, int | float] | None = None
    values: list[str] | None = None
    format: str | None = None
    help: str | None = None
    ops_only: bool = False
    read_cadence: str | None = None
    enforce: bool = False
    # Consumer-side hint that this key carries sensitive material and
    # must be stored + served encrypted (Stra2us per-record encrypted
    # flag, ext type 0x21 wire format). Stra2us itself does not act on
    # this field — it's a declarative pairing for consumer drift tests
    # ("every catalog `encrypted: true` is actually stored encrypted")
    # and name-pattern lints. See docs/fr_encrypted_values.md
    # ("Catalog hint" section) for the full rationale.
    encrypted: bool = False
    # Customer-facing title for the `/app/<app>/<device>` UI (see
    # docs/fr_application_view.md). **Presence is the visibility gate**:
    # a var with `label` shows up in the customer's settings list, a
    # var without one is hidden. Distinct from `help` — `label` is a
    # few words for the title, `help` is a sentence for the description.
    # Operator-jargon vars (`debug_flag_experimental`, perf knobs) just
    # don't get a label and stay admin-only. Stra2us itself does not
    # act on this field; it's consumed by the app-view JS.
    label: str | None = None

    @field_validator("label")
    @classmethod
    def _label_nonempty(cls, v: str | None) -> str | None:
        # Empty-string labels would render as a blank card title in the
        # customer UI — almost certainly a mistake. Reject so operators
        # either commit to a real title or omit the field entirely.
        if v is not None and not v.strip():
            raise ValueError("`label` must be non-empty if provided (omit it to hide the var from /app)")
        return v

    @field_validator("scope")
    @classmethod
    def _unique_scope(cls, v: list[Scope]) -> list[Scope]:
        if len(set(v)) != len(v):
            raise ValueError("scope entries must be unique")
        return v

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "Var":
        t = self.type

        # At most one of {default, default_per_device, default_per_platform}.
        # Each flag signals a different origin for the compiled-in default:
        #   - `default`              : lives in the catalog as a literal
        #   - `default_per_device`   : lives in per-device headers (one per unit)
        #   - `default_per_platform` : lives in per-HAL source (one per platform)
        # Zero-set is legal (ops_only keys like `ir` have no literal default).
        set_origins = [
            name for name, is_set in (
                ("default", self.default is not None),
                ("default_per_device", self.default_per_device),
                ("default_per_platform", self.default_per_platform),
            ) if is_set
        ]
        if len(set_origins) > 1:
            raise ValueError(
                "at most one of `default`, `default_per_device: true`, "
                f"`default_per_platform: true` may be set (got: {set_origins})"
            )

        # `range` is numeric-only.
        if self.range is not None:
            if t not in ("int", "float"):
                raise ValueError(f"`range` is only valid for int/float (got {t})")
            lo, hi = self.range
            if lo > hi:
                raise ValueError(f"`range` lo > hi ({lo} > {hi})")

        # `values` is enum-only, and required for enum.
        if t == "enum":
            if not self.values:
                raise ValueError("`type: enum` requires `values: [...]`")
            if len(set(self.values)) != len(self.values):
                raise ValueError("`values` entries must be unique")
            if self.default is not None and self.default not in self.values:
                raise ValueError(
                    f"default {self.default!r} not in values {self.values}"
                )
        elif self.values is not None:
            raise ValueError(f"`values` is only valid for type: enum (got {t})")

        # Type-match on default.
        if self.default is not None:
            if t == "int" and not isinstance(self.default, int):
                raise ValueError(f"default for int must be int, got {type(self.default).__name__}")
            if t == "float" and not isinstance(self.default, (int, float)):
                raise ValueError(f"default for float must be number, got {type(self.default).__name__}")
            if t == "string" and not isinstance(self.default, str):
                raise ValueError(f"default for string must be string, got {type(self.default).__name__}")
            if t == "bool" and not isinstance(self.default, bool):
                raise ValueError(f"default for bool must be bool, got {type(self.default).__name__}")
            if t == "enum" and not isinstance(self.default, str):
                raise ValueError(f"default for enum must be string, got {type(self.default).__name__}")

        # Range-match on default for numeric types.
        if self.range is not None and isinstance(self.default, (int, float)) \
                and not isinstance(self.default, bool):
            lo, hi = self.range
            if self.default < lo or self.default > hi:
                raise ValueError(
                    f"default {self.default} outside range [{lo}, {hi}]"
                )

        return self


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str
    vars: dict[str, Var]
    version: int = 1

    # App-level fields driving the `/app/<app>/<device>` customer view
    # (see docs/fr_application_view.md). Both optional — apps that
    # don't customize get sensible defaults from the app view's JS.
    #
    # `telemetry_topic`: which queue to tail for status / activity.
    # Supports `{app}` and `{device}` placeholders. Default applied at
    # the consumer side: `{app}/public/heartbeep`.
    #
    # `heartbeat_interval_seconds`: app's expected telemetry cadence.
    # Drives the customer view's status-badge thresholds (Online if
    # `< 2× interval` since last message, Recently active if
    # `< 20× interval`, otherwise Offline). Default applied at the
    # consumer side: 60s. Set explicitly to match your firmware's
    # actual cadence so a 5-min-cadence device isn't called Offline at
    # 4 minutes since last message.
    telemetry_topic: str | None = None
    heartbeat_interval_seconds: int | None = None

    @field_validator("app")
    @classmethod
    def _app_shape(cls, v: str) -> str:
        if not APP_NAME_RE.match(v):
            raise ValueError(
                f"app name {v!r} must match {APP_NAME_RE.pattern}"
            )
        return v

    @field_validator("vars")
    @classmethod
    def _var_names(cls, v: dict[str, Var]) -> dict[str, Var]:
        if not v:
            raise ValueError("`vars` must contain at least one entry")
        for name in v:
            if not VAR_NAME_RE.match(name):
                raise ValueError(
                    f"variable name {name!r} must match {VAR_NAME_RE.pattern}"
                )
        return v

    @field_validator("telemetry_topic")
    @classmethod
    def _telemetry_topic_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("`telemetry_topic` must be non-empty if provided")
        # No `q:` prefix allowed — that's a stra2us-internal Redis-key
        # detail. The catalog declares the topic name as devices use it
        # (`<app>/public/heartbeep`), not as Redis stores it.
        if v.startswith("q:") or v.startswith("kv:"):
            raise ValueError(
                f"`telemetry_topic` is a topic name, not a Redis key — "
                f"drop the {v.split(':', 1)[0]!r} prefix"
            )
        # Leading/trailing slashes almost always indicate a paste mistake.
        if v.startswith("/") or v.endswith("/"):
            raise ValueError(
                "`telemetry_topic` should not start or end with `/`"
            )
        return v

    @field_validator("heartbeat_interval_seconds")
    @classmethod
    def _heartbeat_positive(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v <= 0:
            raise ValueError(
                "`heartbeat_interval_seconds` must be positive (got "
                f"{v!r})"
            )
        return v


# ----- loading -----

class CatalogError(RuntimeError):
    """Parse / schema-validation failure. Messages include the offending path."""


def load_catalog(path: Path) -> Catalog:
    """Parse a YAML catalog and return the validated model.

    Raises CatalogError on file-not-found, YAML parse errors, or schema
    violations. The error message includes the field path where possible
    so the author can find it without re-reading the spec.
    """
    if not path.is_file():
        raise CatalogError(f"catalog not found: {path}")
    try:
        with path.open("r") as fh:
            doc = yaml.safe_load(fh)
    except yaml.YAMLError as e:
        raise CatalogError(f"{path}: YAML parse error: {e}") from e
    if not isinstance(doc, dict):
        raise CatalogError(f"{path}: top-level must be a mapping")
    try:
        return Catalog.model_validate(doc)
    except ValidationError as e:
        lines = [f"{path}: schema validation failed:"]
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            lines.append(f"  {loc}: {err['msg']}")
        raise CatalogError("\n".join(lines)) from e


# ----- value coercion -----

_TRUE = {"true", "1", "yes", "y", "on"}
_FALSE = {"false", "0", "no", "n", "off"}


def coerce_value(var: Var, raw: str, name: str = "<value>") -> object:
    """Parse a raw CLI string against the variable's declared type.

    Returns a native Python value suitable for msgpack encoding:
      int    → int
      float  → float
      string → str
      bool   → bool
      enum   → str (validated against values)

    Raises CatalogError with a human-readable message on type or range
    mismatch. Does **not** consult `enforce:` — that's a server-side
    concern; the CLI always validates locally.
    """
    t = var.type
    if t == "int":
        try:
            v: object = int(raw)
        except ValueError as e:
            raise CatalogError(f"{name}: expected int, got {raw!r}") from e
    elif t == "float":
        try:
            v = float(raw)
        except ValueError as e:
            raise CatalogError(f"{name}: expected float, got {raw!r}") from e
    elif t == "bool":
        s = raw.strip().lower()
        if s in _TRUE:
            v = True
        elif s in _FALSE:
            v = False
        else:
            raise CatalogError(
                f"{name}: expected bool-ish (true/false/1/0/yes/no), got {raw!r}"
            )
    elif t == "enum":
        assert var.values is not None  # schema guarantees
        if raw not in var.values:
            raise CatalogError(
                f"{name}: {raw!r} not in allowed values {var.values}"
            )
        v = raw
    else:  # string
        v = raw

    if var.range is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
        lo, hi = var.range
        if v < lo or v > hi:
            raise CatalogError(
                f"{name}: value {v} outside catalog range [{lo}, {hi}]"
            )
    return v


def kv_path(app: str, key: str, device: str | None) -> str:
    """Build the KV key for a given scope. `device=None` → app scope.

    App-scope writes land under `<app>/public/<key>` (the public/
    namespace convention from docs/fr_application_view.md). This is
    what makes the customer's narrow ACL (`<app>/<device>:rw` +
    `<app>/public:r`) able to *read* app-scope defaults without
    granting them cross-device read.

    Per-device writes are unchanged — devices keep using their own
    `<app>/<device>/<key>` paths and don't need to know about public/.

    *Migration dependency:* this returns the new path unconditionally,
    so any pre-migration deployment must complete the operator-side
    data move (`kv:<app>/<key>` → `kv:<app>/public/<key>`) before
    deploying this code, or app-scope writes/reads will land at the
    new path while old data sits at the old. See the firmware-team
    brief in `fr_application_view.md`.
    """
    if device is None:
        return f"{app}/public/{key}"
    return f"{app}/{device}/{key}"
