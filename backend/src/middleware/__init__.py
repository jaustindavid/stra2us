# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
"""Cross-cutting HTTP middleware.

Today this package holds the CSP middleware
(`docs/fr_catalog_app_ui.md` "Content Security Policy" + the rollout
plan in P0/P5 of `docs/fr_catalog_app_ui_plan.md`). The legacy
inline middleware in `backend/src/main.py` (admin auth, activity
log, perf log) stays in place; new middleware lands here.
"""
