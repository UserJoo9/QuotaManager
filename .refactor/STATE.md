# .refactor/STATE.md
# Total Project Refactor — Orchestrator State
# Branch: refactor/total (created from main @ pre-refactor tag)
# Language: Egyptian Arabic for reporting

## Current Phase
Phase 3 COMPLETE — all 17 units done (15 commits from ab33a35 to 8683d80).

## Wave
Wave A — Backend foundations U1/U2/U3: DONE (3 commits: ab33a35, 7078c95, 30533eb)
Wave B — Backend foundations U4/U5/U6: DONE (3 commits: 19fc1ba, 0ebb142, d581979)
Wave C — Backend API + runtime U7/U8: DONE (2 commits: d4076f3, 6169894)
Wave D — Frontend U9/U10/U11: DONE (3 commits: d3d5310, 98f105c, 6fca2bf)
Wave E — Frontend U12/U13: DONE (2 commits: 693dbf9, c6cbe85)
Wave F — U14 design system: DONE (1 commit: f588ece)
Wave G — U15 tests & safety net: DONE (1 commit: 8683d80)

## Per-Unit Status
- U1 (config+db+service): DONE — ab33a35
- U2 (nftables+firewall): DONE — 7078c95
- U3 (shaping+vpnshare): DONE — 30533eb
- U4 (history+analytics+DNS+ARP): DONE — 19fc1ba
- U5 (IPv6+network+VPN/TUN+wifi/latency): DONE — 0ebb142
- U6 (storage+security+__init__): DONE — d581979
- U7 (backend API layer): DONE — d4076f3
- U8 (runtime entry run.py): DONE — 6169894
- U9 (frontend config→transport→model): DONE — d3d5310
- U10 (frontend port→api→services): DONE — 98f105c
- U11 (frontend state/hooks/ui/shared/app): DONE — 6fca2bf
- U12 (frontend pages): DONE — 693dbf9 (migrated security/securityDns raw fetch to hooks, added getTls/getPresets/getRules)
- U13 (frontend components): DONE — c6cbe85 (JSDoc types, named constants, shared focusable selector)
- U14 (UI/CSS/HTML design system): DONE — f588ece (consolidated design tokens, removed duplicate CSS, verified RTL/LTR)
- U15 (tests & safety net): DONE — 8683d80 (characterization tests + E2E stubs, G0-G7 gates)

## Last Green Commit
HEAD = refactor/total (8683d80). Full suite: 941 passed (baseline 920 + 21 new), 3 warnings. pyflakes clean, node --check OK on all web/js/**. test_new_ui.py 16/16. Working tree clean except bin/sing-box (mode change only, outside allowlist) and .refactor/STATE.md.

## Open Blockers
None

## Next Action
Phase 3 complete. Phase 4 (merge to main, final verification, PR) pending user decision.

## Known Bugs — ALL FIXED (commit e40c058)
1. ✅ `hooks/index.js` `useShare` — added `createShareService` import + instantiation
2. ✅ `chart.js` duplicate `#barGrad` IDs — scoped to `barGrad${id}` per chart instance
3. ✅ `chart.js` zero-max division → NaN — `yOf` + `barChart` guard for `max <= 0`
4. ✅ `dialog.js`/`drawer.js` Escape/Tab handlers — extracted shared `onKeyTrap` in `core/dom.js`
5. ✅ `shared/formatBytes` NaN for negative bytes — early-return guard
6. ✅ `logging_setup.py` double ERROR emit — removed redundant `console_errors` handler
7. ✅ `history_analytics.py` top_consumer zero-traffic — active-only selection via `next()`
8. ✅ `flows.py` available=True when conntrack empty — explicit `False` guard

## Last Green Commit
HEAD = refactor/total (e40c058). Full suite: 941 passed, 6 warnings in 510.65s. pyflakes clean, node --check OK on all web/js/**. test_new_ui.py 16/16.

## Notes
- Known bug from U11: `hooks/index.js` `useShare` references `share` never instantiated. Fix in separate `fix:` commit.
- U13 bugs logged (not fixed): chart.js duplicate `#barGrad` IDs; chart.js zero-max division; dialog/drawer key handlers near-duplicates.
- Constitution: .refactor/specs/memory/constitution.md. Plan: .refactor/01-plan.md (17 units, 7 waves).
- Master prompt constraints: one commit per task, conventional commits, Egyptian Arabic reporting, never touch main, no force-push.

## Policy Engine (Phase 4) — ADDED THIS SESSION
- `quota/policy_engine.py` — Policy model, evaluation, conflict resolution (deny-wins), simulation
- `quota/app_catalog.py` — 20+ apps (TikTok, Instagram, YouTube, etc.) with features + capability matrix
- `quota/capabilities.py` — Runtime detection of DNS/firewall/tc/proxy/agent support
- `quota/policy_adapters.py` — Pluggable enforcement adapters (DNS, firewall, tc, quota)
- `quota/policy_scheduler.py` — Time-based schedule evaluation
- `api/policy_routes.py` — REST API: CRUD, evaluate, simulate, capabilities, catalog, enforcement status
- `api/app.py` — Policy engine initialized at startup, routes mounted at /api/policies/*
- `web/js/pages/policy.js` — Frontend Policy Builder UI (list, create, edit, delete, toggle, simulate, templates)
- `web/js/i18n/en.js` + `web/js/i18n/ar.js` — Arabic + English translations
- `quota/db.py` — policies table + policy_audit_log table + CRUD + audit logging
- `tests/test_policy_engine.py` — 17 tests (engine + DB CRUD)
