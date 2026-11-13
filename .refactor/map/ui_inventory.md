# UI/CSS/HTML inventory map — QuotaManager web/
# Source: /home/abed/Projects/QuotaManager/web ; verified with ls + file reads, 2026-11-11

## Asset inventory (path | size | purpose)

### web/css/
- css/tokens.css — 2,084 B — design tokens: fonts (Inter, QM Arabic, JetBrains Mono), dark/light colors, spacing (--pad --gap --rowpad --navh), direction (--dir), density/fx themes
- css/base.css — 13,382 B — reset, atoms (bar, chip, btn, icon-btn, sw, seg, dot, skeleton, empty), focus rings, skip link, user-select/drag suppression, RTL flips, mobile touch targets
- css/layout.css — 7,288 B — shell/sidebar/nav/main grid, off-canvas mobile sidebar, collapsed rail, print styles, RTL border/inset overrides
- css/components.css — 33,458 B — hero/gauge, tiles, feed, people rows, drawer, toast, dialog, forms, VPN nodes, tabs, system settings, login, compat aliases

### web/assets/
- assets/pub.css — 4,593 B — shared styles for public pages (milestone/report/guest_pending/spoof)
- assets/share.css — 14,491 B — /share page (Local Share file/text transfer)
- assets/detector.js — 17,052 B — anti-devtool / spoof detection (spoof.html)
- assets/qrcode.min.js — 19,927 B — vendor QR library
- assets/i18n-share.js — 10,376 B — /share page strings
- assets/i18n-milestone.js — 2,382 B — /milestone page strings
- assets/pub-i18n.js — 4,039 B — public i18n loader
- assets/pub-theme.js — 1,109 B — theme/accent applier for public pages (localStorage prefs)
- assets/logo.png — 1,800,513 B — brand logo (1.8 MB, oversized for web)

### web/*.html
- index.html — 930 B — SPA shell (mounts #root via js/main.js), skip link, font preloads
- share.html — 87,437 B — Local Share page (largest; inline contextmenu/dragstart suppression)
- milestone.html — 14,836 B — public quota-remaining view
- report.html — 10,770 B — read-only consumption report
- guest_pending.html — 17,273 B — RTL Arabic guest approval page (inline token redefinitions)
- spoof.html — 17,428 B — Usage Calibration Studio (loads detector.js)

### web/js/i18n/
- js/i18n/ar.js — 56,325 B — 989 keys, Egyptian Arabic
- js/i18n/en.js — 43,538 B — 989 keys, English — key sets identical (0 missing either way)

## Design tokens (css/tokens.css)
- Color: --accent #5B8CFF, --accent-ink #06101F, --accent-2 #5EEAD4; --bg/--surface/--surface-2/--surface-3; --line/--line-2; --text/--text-2/--text-3; --ok #3DDC97, --warn #F5B544, --danger #FF6B7A; --shadow; --scrim
- Spacing: --pad 22px, --gap 18px, --rowpad 14px, --navh 42px (compact: 16/14/9/36 via data-density=compact)
- Type: --font = Inter + "QM Arabic" fallback; --mono = JetBrains Mono; base 14px / 1.5
- Direction: --dir: 1 (ltr) / -1 (rtl via [dir=rtl]); fonts reordered for Arabic
- Themes: data-theme=dark|light; data-fx=off|subtle|rich; color-scheme honored

## RTL / i18n handling
- ar.js/en.js parity confirmed: 989 keys each, identical key sets (verified programmatically)
- RTL: tokens.css flips --font order + --dir; layout.css uses logical props (inset-inline/border-inline) + explicit RTL overrides (.nav-glabel, .nav-i.on::before, .drawer); base.css .flip/.chev/.collapse-btn use scaleX(var(--dir)); components.css RTL letter-spacing + .swatch padding flips
- Gaps: guest_pending.html redefines token values inline (diverges from tokens.css); public pages (share/milestone/report/spoof) lack dir attr; index.html only page with dir="ltr"; RTL not applied to .gauge/.kpi-grid numeric letter-spacing (handled case-by-case)

## Dead CSS candidates (unverified — confirm via coverage tool before removal)
- .skeleton, .empty, .dot, .bar, .pill (7 hits), .badge-dot (3 hits) — low HTML/JS hit counts
- Ambient .fx/.blob layer (controlled by data-fx; verify it is toggled)

## Duplicated components
- .btn: defined in base.css, pub.css, share.css (3 implementations; slight border/background variance)
- .card: layout.css vs pub.css (same border/shadow/radius pattern)
- .toast: components.css vs share.css (near-identical)
- .banner: components.css vs pub.css (same pattern, different token naming)
- .chip (base.css) vs .pill (components.css compat) — same pill pattern, two names
- :focus-visible rules repeated 2× in base.css; reduced-motion + user-select/cursor blocks repeated across base.css, pub.css, share.css; mobile touch-target media queries duplicated base.css ↔ components.css

## a11y gaps
- Skip link only in index.html; missing on share/milestone/report/guest_pending/spoof
- .toasts / .toast: no role="status" or aria-live (visual-only announcements)
- Dialogs (.dialog): verify role="dialog"/aria-modal in JS-mounted pages
- :invalid input styling without guaranteed aria-invalid/required association
- guest_pending.html inline tokens bypass theme system (color-scheme set manually)
- Reduced motion + 44px touch targets + focus rings present and consistent

## Responsive gaps
- Breakpoints: 1180 / 1100 / 900 / 640 / 480 px; grids collapse, side drawer goes off-canvas, touch targets floor at 44px, font floors applied
- Fixed heights not fully converted: .gauge 190px, .chart 190px, .live-body 390px (max-height override exists), .terminal 260px (max-height override exists)
- .search min-width:240px (overridden to 100% at 640px); .nav-i nowrap may overflow in collapsed rail at very small widths
