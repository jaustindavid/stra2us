"""Credential / host lookup for the CLI.

Resolution order (first hit wins):

    1. explicit --server / --client-id / --secret flags
    2. env vars STRA2US_HOST / STRA2US_CLIENT_ID / STRA2US_SECRET_HEX
    3. ./.stra2us  (project-local)
    4. ~/.stra2us  (user-global)

Steps 3-4 are TOML files with sections per profile. Default section is
`[default]`; `--profile <name>` selects `[profile.<name>]`.

    [default]
    host = "stra2us.example.com:8153"
    client_id = "dev"
    secret_hex = "0123...cafe"

    [profile.prod]
    host = "stra2us.prod.example.com:8153"
    client_id = "ops"
    secret_hex = "deadbeef..."

A future `stra2us login` subcommand will write ~/.stra2us with
0600 perms. For now, users create it by hand.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore


CONFIG_FILENAME = ".stra2us"


class ConfigError(RuntimeError):
    """Missing or malformed credentials."""


@dataclass
class Credentials:
    host: str          # full URL, "http://host:port" — normalized
    client_id: str
    secret_hex: str
    source: str        # human-readable origin: "flag", "env", "~/.stra2us[prod]", ...

    @property
    def base_url(self) -> str:
        return self.host


def _normalize_host(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw


def _read_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _section(doc: dict, profile: str | None) -> dict:
    """Pick the section for `profile`. `None` → `[default]`."""
    if profile is None:
        return doc.get("default") or {}
    return (doc.get("profile") or {}).get(profile) or {}


def _load_file_creds(profile: str | None) -> tuple[dict, str] | None:
    """Walk ./.stra2us then ~/.stra2us; return (section_dict, source_label)
    for the first file that has a matching section, else None."""
    candidates = [
        (Path.cwd() / CONFIG_FILENAME, f"./{CONFIG_FILENAME}"),
        (Path.home() / CONFIG_FILENAME, f"~/{CONFIG_FILENAME}"),
    ]
    for path, label in candidates:
        if not path.is_file():
            continue
        try:
            doc = _read_toml(path)
        except Exception as e:
            raise ConfigError(f"{label}: parse error: {e}") from e
        section = _section(doc, profile)
        if section:
            tag = f"[{profile}]" if profile else "[default]"
            return section, f"{label} {tag}"
    return None


def resolve(
    server: str | None = None,
    client_id: str | None = None,
    secret_hex: str | None = None,
    profile: str | None = None,
) -> Credentials:
    """Resolve credentials per the documented order. Raises ConfigError
    if any of the three fields is missing after all sources are exhausted."""

    # File lookup (may or may not contribute — flags/env still win per-field).
    file_creds: dict = {}
    file_source = ""
    got = _load_file_creds(profile)
    if got is not None:
        file_creds, file_source = got

    def pick(
        flag_val: str | None,
        env_key: str,
        file_key: str,
    ) -> tuple[str | None, str]:
        if flag_val:
            return flag_val, "flag"
        ev = os.environ.get(env_key)
        if ev:
            return ev, f"${env_key}"
        fv = file_creds.get(file_key)
        if fv:
            return fv, file_source
        return None, ""

    host_v, host_src = pick(server, "STRA2US_HOST", "host")
    cid_v,  cid_src  = pick(client_id, "STRA2US_CLIENT_ID", "client_id")
    sec_v,  sec_src  = pick(secret_hex, "STRA2US_SECRET_HEX", "secret_hex")

    missing = []
    if not host_v: missing.append("host (--server / $STRA2US_HOST / .stra2us:host)")
    if not cid_v:  missing.append("client_id (--client-id / $STRA2US_CLIENT_ID / .stra2us:client_id)")
    if not sec_v:  missing.append("secret (--secret / $STRA2US_SECRET_HEX / .stra2us:secret_hex)")
    if missing:
        raise ConfigError("missing credentials: " + "; ".join(missing))

    # Per-field source label; compact when everything came from one place.
    sources = {host_src, cid_src, sec_src}
    source_label = next(iter(sources)) if len(sources) == 1 else \
        f"host={host_src}, client_id={cid_src}, secret={sec_src}"

    return Credentials(
        host=_normalize_host(host_v),  # type: ignore[arg-type]
        client_id=cid_v,               # type: ignore[assignment]
        secret_hex=sec_v,              # type: ignore[assignment]
        source=source_label,
    )
