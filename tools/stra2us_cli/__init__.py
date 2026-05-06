"""Reference CLI for Stra2us KV catalogs.

See docs/catalog_spec.md in the stra2us repo for the catalog schema
and resolution contract. The CLI is a thin shell over three concerns:

    catalog.py  — YAML loader + schema validation (pydantic)
    client.py   — HMAC-signed HTTP client for the stra2us server
    config.py   — credential / host lookup (flag → env → .stra2us)
    cli.py      — argparse + verb dispatch

Python consumers should import from this package root rather than the
internal modules — the names below are the supported public surface;
anything else (private helpers in cli/config/etc.) is fair game to
change between minor versions.
"""

from .client import Stra2usClient, Stra2usError
from .config import resolve as resolve_credentials, ConfigError


def client_from_env(
    *,
    server: str | None = None,
    client_id: str | None = None,
    secret_hex: str | None = None,
    profile: str | None = None,
) -> Stra2usClient:
    """Build a Stra2usClient using the same flag → env → profile lookup
    chain the CLI uses. Any explicitly-passed argument wins; missing
    arguments fall through to STRA2US_* env vars and the named profile
    in `~/.stra2us` (or `./.stra2us`). Raises ConfigError if a required
    field can't be resolved."""
    creds = resolve_credentials(
        server=server,
        client_id=client_id,
        secret_hex=secret_hex,
        profile=profile,
    )
    return Stra2usClient(
        base_url=creds.base_url,
        client_id=creds.client_id,
        secret_hex=creds.secret_hex,
    )


__all__ = [
    "Stra2usClient",
    "Stra2usError",
    "ConfigError",
    "resolve_credentials",
    "client_from_env",
]

__version__ = "0.1.0"
