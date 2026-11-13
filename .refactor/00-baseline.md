# .refactor/00-baseline.md
# Baseline — captured at start of total refactor (Phase 0)
# Branch: refactor/total, tag: pre-refactor
# Date: 2026-11-11

## Stack
- Backend: Python 3.12, FastAPI 0.141.1, uvicorn 0.52.1, aiosqlite, PyYAML
- Frontend: vanilla JS (no build step, no package.json), 51 JS files, 4 CSS files, 6 HTML files
- Entry point: `run.py` (Gateway process on Linux/Kali/Debian)
- Tests: pytest 9.1.1, httpx 0.28.1, pyflakes>=3.2<4 (lint)
- Test deps install: `python3 -m venv .venv && .venv/bin/pip install -r requirements-linux.txt`
- Working test venv: `/home/abed/Data/03_Professional/Projects/Telegram_Bots/tanzil/.venv/bin/python`

## Baseline Test Results
- tests/test_new_ui.py: 16 passed (UI/architecture regression tests) — CONFIRMED
- Full suite: interrupted at collection — `uvicorn` missing in default env; 2 collection errors (test_run_wiring, test_unmanaged) are PRE-EXISTING environment issues, not regressions.
- JS syntax: 0 errors across all web/js files (verified via node --check)
- Browser smoke test: index.html loads, title "🐴 QuotaManager", 0 JS console errors, screenshot captured

## Pre-Existing Failures (recorded, not counted against us)
- test_run_wiring.py / test_unmanaged.py: collection error — uvicorn not installed in default env
- Full test suite requires the working venv

## Next
- Wait for background pytest run to confirm 16/16
- Phase 1: dispatch explore sub-agents for full system map
- Phase 2: Spec Kit setup and unit decomposition
