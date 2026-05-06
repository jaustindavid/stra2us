"""Tests for credential resolution."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from stra2us_cli.config import ConfigError, resolve


def _write_config(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body))


def test_flags_win(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRA2US_HOST", "env-host:8153")
    monkeypatch.setenv("STRA2US_CLIENT_ID", "env-id")
    monkeypatch.setenv("STRA2US_SECRET_HEX", "ef" * 32)
    creds = resolve(
        server="flag-host:8153",
        client_id="flag-id",
        secret_hex="ab" * 32,
    )
    assert creds.base_url == "http://flag-host:8153"
    assert creds.client_id == "flag-id"
    assert creds.secret_hex == "ab" * 32
    assert "flag" in creds.source


def test_env_when_no_flags(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRA2US_HOST", "env-host:8153")
    monkeypatch.setenv("STRA2US_CLIENT_ID", "env-id")
    monkeypatch.setenv("STRA2US_SECRET_HEX", "cd" * 32)
    creds = resolve()
    assert creds.base_url == "http://env-host:8153"
    assert creds.client_id == "env-id"


def test_https_preserved(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRA2US_HOST", raising=False)
    monkeypatch.delenv("STRA2US_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRA2US_SECRET_HEX", raising=False)
    creds = resolve(
        server="https://secure.example.com",
        client_id="x",
        secret_hex="00" * 32,
    )
    assert creds.base_url == "https://secure.example.com"


def test_project_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRA2US_HOST", raising=False)
    monkeypatch.delenv("STRA2US_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRA2US_SECRET_HEX", raising=False)
    _write_config(tmp_path / ".stra2us", """
        [default]
        host = "file-host:8153"
        client_id = "file-id"
        secret_hex = "11111111111111111111111111111111111111111111111111111111111111ff"
    """)
    # Point HOME elsewhere so we don't accidentally read the real user file.
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    creds = resolve()
    assert creds.base_url == "http://file-host:8153"
    assert creds.client_id == "file-id"
    assert ".stra2us" in creds.source


def test_profile_selection(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRA2US_HOST", raising=False)
    monkeypatch.delenv("STRA2US_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRA2US_SECRET_HEX", raising=False)
    _write_config(tmp_path / ".stra2us", f"""
        [default]
        host = "dev-host:8153"
        client_id = "dev"
        secret_hex = "{"aa" * 32}"

        [profile.prod]
        host = "prod-host:8153"
        client_id = "prod"
        secret_hex = "{"bb" * 32}"
    """)
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    creds = resolve(profile="prod")
    assert creds.base_url == "http://prod-host:8153"
    assert creds.client_id == "prod"


def test_missing_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRA2US_HOST", raising=False)
    monkeypatch.delenv("STRA2US_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRA2US_SECRET_HEX", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    with pytest.raises(ConfigError, match="missing credentials"):
        resolve()
