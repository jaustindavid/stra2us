# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Tests for the markdown rendering cache (P3).

Two concerns:

1. Functional — `render_block` returns the same HTML for the same
   `(app, publish_hash, block_id)` key.
2. Cache discipline — repeated calls don't re-invoke the
   sanitizer; publish_hash bumps invalidate; per-app and
   per-block_id keys are independent.
"""

from __future__ import annotations

import pytest

# `markdown_render` was a vendored copy of the CLI's sanitizer
# until P5 followup #2. Now `markdown_cache` imports
# `sanitize_markdown` from `stra2us_cli.sanitizers.markdown`
# directly. Tests that monkeypatch the sanitizer patch the
# *symbol the module imported into its own namespace*, not the
# canonical source — same shape as the routes_app_assets /
# routes_app_theme fixtures.
from services import markdown_cache


@pytest.fixture(autouse=True)
def reset_cache():
    markdown_cache.clear()
    yield
    markdown_cache.clear()


@pytest.fixture
def count_sanitize_calls(monkeypatch):
    """Wrap `sanitize_markdown` to count invocations. Lets tests
    assert "the cache short-circuited the second call."""
    counter = {"n": 0}
    real = markdown_cache.sanitize_markdown

    def wrapped(source, *, app, max_bytes=None):
        counter["n"] += 1
        return real(source, app=app, max_bytes=max_bytes)

    monkeypatch.setattr(markdown_cache, "sanitize_markdown", wrapped)
    return counter


# ----- functional -----

def test_renders_known_good_markdown(count_sanitize_calls):
    out = markdown_cache.render_block(
        app="demo", publish_hash="abc", block_id="header",
        source="## Hello\n\nWorld.",
    )
    assert "<h2>Hello</h2>" in out
    assert "<p>World.</p>" in out


def test_image_in_markdown_resolves_to_app_assets_path(count_sanitize_calls):
    """Inline `<img>` references in markdown should rewrite to the
    same-origin per-app asset URL — same behavior as the
    canonical sanitizer (`tools/stra2us_cli/sanitizers/markdown.py`)."""
    out = markdown_cache.render_block(
        app="critterchron", publish_hash="abc", block_id="header",
        source="![logo](logo.svg)",
    )
    assert 'src="/app/critterchron/_assets/logo.svg"' in out


# ----- cache discipline -----

def test_repeated_call_with_same_key_uses_cache(count_sanitize_calls):
    """The whole point — second call doesn't re-run the sanitizer."""
    for _ in range(5):
        markdown_cache.render_block(
            app="demo", publish_hash="abc", block_id="header",
            source="## Hi",
        )
    assert count_sanitize_calls["n"] == 1
    stats = markdown_cache.stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 4


def test_publish_hash_bump_invalidates(count_sanitize_calls):
    """Republish bumps publish_hash → fresh cache key → sanitizer
    runs again. The new entry coexists with the old; old entry is
    unreachable until process restart but doesn't leak per request."""
    markdown_cache.render_block(
        app="demo", publish_hash="abc", block_id="header", source="## A",
    )
    markdown_cache.render_block(
        app="demo", publish_hash="def", block_id="header", source="## B",
    )
    assert count_sanitize_calls["n"] == 2


def test_per_block_id_keys_are_independent(count_sanitize_calls):
    """Header and footer share `(app, publish_hash)` but different
    block_id → independent cache entries → both sanitize."""
    markdown_cache.render_block(
        app="demo", publish_hash="abc", block_id="header",
        source="## Header",
    )
    markdown_cache.render_block(
        app="demo", publish_hash="abc", block_id="footer",
        source="## Footer",
    )
    assert count_sanitize_calls["n"] == 2


def test_per_app_keys_are_independent(count_sanitize_calls):
    """Different apps with identical block content + hash still
    cache separately — the asset URL rewrite path embeds the app
    name, so byte-equivalent input produces app-keyed output."""
    markdown_cache.render_block(
        app="appa", publish_hash="abc", block_id="header",
        source="![logo](logo.svg)",
    )
    markdown_cache.render_block(
        app="appb", publish_hash="abc", block_id="header",
        source="![logo](logo.svg)",
    )
    assert count_sanitize_calls["n"] == 2


def test_clear_resets_state(count_sanitize_calls):
    markdown_cache.render_block(
        app="demo", publish_hash="abc", block_id="header", source="## Hi",
    )
    markdown_cache.clear()
    markdown_cache.render_block(
        app="demo", publish_hash="abc", block_id="header", source="## Hi",
    )
    assert count_sanitize_calls["n"] == 2
    assert markdown_cache.stats()["hits"] == 0


def test_stats_count_initial_zero():
    assert markdown_cache.stats() == {"hits": 0, "misses": 0}
