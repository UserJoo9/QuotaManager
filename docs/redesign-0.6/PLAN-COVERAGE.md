# Redesign 0.6 — coverage of the Notion plan

Plan: *QuotaManager — Complete Premium UX & Dashboard Redesign Plan* (sections 0–9, including
§8 performance), with the architecture decision React + TypeScript + Vite, Tailwind + OKLCH
tokens, selective Radix primitives, TanStack Query, dnd-kit, Python backend unchanged.
Additions requested during the work: no "Customize sidebar" mode, error pages, empty states
with illustrations, passkeys (Face ID / Windows Hello, device-bound).

**Result: every implementation item of the plan is done.** Three *verification* items ask for
physical hardware that a cloud sandbox does not have; they were covered by emulation and are
listed at the end as the remaining sign-off on real devices.

| Plan section | Items | Done | Evidence |
|---|---:|---:|---|
| 1. AI-looking UI audit | 9 | 9 | neutral OKLCH palette, blobs/particles/glows removed, one icon family, density rules, Arabic pass over every page (stiff MSA phrasing removed, "node" → "server") |
| 2. 12 premium UX details | 11 (+1 N/A) | 11 | trust copy (login, Tailscale, share, about), empty states with next step + illustration, glossary + dotted terms, confirmations after server ack, plain-language errors, "what we're waiting for" loaders, remembered prefs/filters/drafts, literal CTAs, Clear filters on People/Insights/Security/DNS/VPN/Logs, critical data never truncated. *N/A: the dashboard asks for no browser permissions.* |
| 3. Dashboard architecture A–F | 22 | 22 | sidebar spine (identity, groups, counts, rail + tooltips, phone drawer), one question per page, primary action in the title bar, tables with search/sort/filter/selection + confirmed bulk actions, line/bar/meter charts with axes, units, hover and text summaries, popover/modal/sheet/toast used per rule, no optimistic gateway changes |
| 4. Three subtle flaws | 15 | 15 | data-driven forms (chips, meters, mono LTR identifiers with copy), progressive disclosure (menus, collapsible advanced VPN shaping, details), invisible states (unsaved, pending, applying→applied, offline, reconnecting, 404, crash, update-reload) |
| 5. Sidebar reordering | 6 | 6 | six-dot handle on hover/focus, drag / long-press / Space+arrows, screen-reader announcements, stable ids + version, new routes slot in, reset (System → Sidebar) |
| 6. Theme (keep-vanilla item superseded by the React decision) | 4 (+1 superseded) | 4 | one canonical light/dark theme, 4 surfaces · 2 strokes · 3 text tiers, semantic colours only, brand kept |
| 7a. Colour videos + token architecture | 21 | 21 | `web/css/tokens.css`, `tests/test_design_tokens.py` (WCAG AA computed for both themes), `docs/redesign-0.6/DESIGN-TOKENS.md` + `tokens.json` (HEX export) |
| 7b. Inputs table | 17 (+1 N/A) | 17 | GB fields with unit/stepper/presets, renewal mode + day (never "0"), pack duration + expiry preview, recurring switch, searchable target pickers, VPN % slider + number, integer guest count, typed firewall ports/IP/CIDR, WAN renew minutes + presets, segmented windows/theme, consequence dialogs. *N/A: no date-range filter exists.* |
| 7c. Input anatomy + full-system targets | 20 | 20 | string drafts, validation on blur, no wheel edits, 16 px phone inputs, pending/disabled/error states; all ten pages redesigned |
| 7d. Mobile sheet + keyboard contract | 17 | 17 | bottom sheet ↔ side panel, visualViewport sizing, focused field kept visible, single scroller, swipe only on the grip, unsaved-changes guard, focus trap/restore |
| 7e. Device acceptance matrix | 7 | 7 in emulation | Pixel 7 + iPhone 13 profiles with on-screen-keyboard simulation (footer stays above keyboard, field scrolled into view, typing, background scroll restored), hardware keyboard (Tab trap, Shift+Tab, Escape restores focus), RTL/LTR, reduced motion, 200 % zoom EN/AR, tablet, 320 px phone, axe-core 0 violations on 40 page×language×theme combinations |
| 7f. Design-system deliverables | 7 | 7 | token spec + HEX export (equivalent to the Figma spec the plan allows), component library, EN/AR × light/dark screenshots, before/after benchmark (phone, tablet, desktop, 200 % zoom), input + colour acceptance tests |
| 8. Performance (8.1–8.9) | 49 | 49 | see the table below; persistent frame, one socket, memory-only cache, immutable precompressed assets, route chunks + idle/intent prefetch, hidden-tab buffering, no decorative animation, soak + payload benchmark |
| 9. Stages 0–7 | 8 | 8 | baseline screenshots + metrics → design system → shell & sidebar → pages → UX rules → inputs & sheet → invisible states & test matrix → release gate (912+ Python tests, Vitest, CI job, a11y, device, perf) |
| **Total** | **213** | **213** | |

## Remaining sign-off on physical hardware (cannot run in a cloud sandbox)

1. A real iPhone (Safari) and a real Android phone (Gboard / Samsung keyboard): the keyboard
   behaviour was verified in emulation only.
2. A screen-reader walkthrough (VoiceOver / TalkBack): automated axe-core checks pass with 0
   violations; a human pass is still recommended.
3. Laptop temperatures: CPU time was measured (idle main thread 0.4 % vs 7.4 % with the old
   default background); temperature sensors are not available in the sandbox.

## Measured performance (old → new)

Headless Chromium, 13 seeded devices, same probe for both builds.

| | LAN | Slow Wi-Fi (1.6 Mbps, 150 ms) |
|---|---|---|
| First contentful paint (cold) | 152 → **72 ms** | 2876 → **1488 ms** |
| First contentful paint (warm) | 104 → **52 ms** | 1476 → **456 ms** |
| Dashboard ready (cold) | 281 → 292 ms | 3690 → **2525 ms** |
| Dashboard ready (warm) | 158 → 163 ms | 2298 → **1256 ms** |
| Transferred, warm reload | 42 → **14 KB** | 43 → **29 KB** |
| Idle main thread (20 s) | 0.61 % → **0.39 %** (old default animated background: **7.37 %**) | |
| DOM nodes on Overview | 10 491 → **3 195** | |
| Route switch → painted | ≈ 30 ms → ≈ 30 ms | |

First load: 106 KB gzip shell + ~20 KB for the one language in use; each page, the
transfer engine and drag-and-drop are separate chunks. Server snapshot (`/api/dashboard`):
8.5 ms at 0 devices, 10.7 ms / 145 KB at 100 devices — no backend change needed.
