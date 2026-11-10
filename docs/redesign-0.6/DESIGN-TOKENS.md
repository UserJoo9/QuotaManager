# Design tokens (generated)

Source of truth: `web/css/tokens.css` (OKLCH). Regenerate with
`.venv/bin/python scripts/export_tokens.py`. HEX values are a compatibility export for design tools.

Contrast is against `--bg-canvas` of the same theme (WCAG 2.x ratio).

## Surfaces

| Token | Dark (OKLCH) | Dark HEX | Light (OKLCH) | Light HEX | Contrast dark / light |
|---|---|---|---|---|---|
| `--bg-frame` | `oklch(0.185 0.006 260)` | `#111315` | `oklch(0.962 0.004 85)` | `#F4F2EF` | 1.04 / 1.06 |
| `--bg-canvas` | `oklch(0.205 0.006 260)` | `#16171A` | `oklch(0.982 0.003 85)` | `#FAF9F7` | 1.00 / 1.00 |
| `--bg-surface` | `oklch(0.235 0.007 260)` | `#1C1E21` | `oklch(1 0 0)` | `#FFFFFF` | 1.07 / 1.05 |
| `--bg-raised` | `oklch(0.275 0.008 260)` | `#25282C` | `oklch(1 0 0)` | `#FFFFFF` | 1.21 / 1.05 |

## Strokes

| Token | Dark (OKLCH) | Dark HEX | Light (OKLCH) | Light HEX | Contrast dark / light |
|---|---|---|---|---|---|
| `--stroke-subtle` | `oklch(0.30 0.008 260)` | `#2B2E32` | `oklch(0.915 0.004 85)` | `#E4E3E0` | 1.31 / 1.22 |
| `--stroke-strong` | `oklch(0.57 0.01 260)` | `#74787D` | `oklch(0.62 0.008 260)` | `#83868B` | 4.01 / 3.46 |

## Text

| Token | Dark (OKLCH) | Dark HEX | Light (OKLCH) | Light HEX | Contrast dark / light |
|---|---|---|---|---|---|
| `--text-primary` | `oklch(0.95 0.004 260)` | `#EDEEF1` | `oklch(0.24 0.01 260)` | `#1C1F24` | 15.48 / 15.62 |
| `--text-secondary` | `oklch(0.82 0.008 260)` | `#C1C4C9` | `oklch(0.40 0.012 260)` | `#44484E` | 10.26 / 8.74 |
| `--text-tertiary` | `oklch(0.70 0.01 260)` | `#9B9FA5` | `oklch(0.50 0.012 260)` | `#5F636A` | 6.71 / 5.70 |

## Accent (functional)

| Token | Dark (OKLCH) | Dark HEX | Light (OKLCH) | Light HEX | Contrast dark / light |
|---|---|---|---|---|---|
| `--accent` | `oklch(0.72 0.10 255)` | `#79A7E2` | `oklch(0.54 0.125 255)` | `#3770B5` | 7.23 / 4.83 |
| `--accent-hover` | `oklch(0.80 0.075 255)` | `#9DC1EE` | `oklch(0.47 0.115 255)` | `#285B9A` | 9.61 / 6.51 |
| `--accent-pressed` | `oklch(0.63 0.12 255)` | `#548BD0` | `oklch(0.39 0.095 255)` | `#1D4576` | 5.11 / 9.15 |
| `--accent-ink` | `oklch(0.18 0.03 255)` | `#08121F` | `oklch(0.99 0.004 255)` | `#FAFCFE` | 1.05 / 1.02 |
| `--accent-soft` | `oklch(0.32 0.04 255)` | `#253447` | `oklch(0.94 0.026 255)` | `#E0ECFD` | 1.41 / 1.13 |
| `--accent-fg` | `oklch(0.80 0.075 255)` | `#9DC1EE` | `oklch(0.47 0.115 255)` | `#285B9A` | 9.61 / 6.51 |
| `--focus` | `oklch(0.80 0.075 255)` | `#9DC1EE` | `oklch(0.63 0.12 255)` | `#548BD0` | 9.61 / 3.33 |

## Semantic

| Token | Dark (OKLCH) | Dark HEX | Light (OKLCH) | Light HEX | Contrast dark / light |
|---|---|---|---|---|---|
| `--success` | `oklch(0.76 0.13 155)` | `#65C98C` | `oklch(0.48 0.11 155)` | `#156F41` | 8.80 / 5.88 |
| `--success-soft` | `oklch(0.30 0.045 155)` | `#1A3424` | `oklch(0.95 0.03 155)` | `#E0F5E6` | 1.34 / 1.09 |
| `--warning` | `oklch(0.82 0.12 80)` | `#EDBB64` | `oklch(0.50 0.11 65)` | `#8D5406` | 10.15 / 5.87 |
| `--warning-soft` | `oklch(0.32 0.045 80)` | `#3F3017` | `oklch(0.95 0.04 80)` | `#FDECD1` | 1.41 / 1.10 |
| `--danger` | `oklch(0.72 0.15 25)` | `#F47B74` | `oklch(0.51 0.17 25)` | `#B32E2F` | 6.77 / 5.96 |
| `--danger-soft` | `oklch(0.31 0.06 25)` | `#4A2321` | `oklch(0.95 0.025 25)` | `#FFE8E6` | 1.33 / 1.11 |
| `--danger-solid` | `oklch(0.58 0.17 25)` | `#CB4644` | `oklch(0.53 0.18 25)` | `#BE2E31` | 3.84 / 5.50 |
| `--info` | `oklch(0.76 0.09 235)` | `#76BBE2` | `oklch(0.50 0.11 240)` | `#0E6A9B` | 8.48 / 5.62 |
| `--info-soft` | `oklch(0.30 0.04 235)` | `#18313F` | `oklch(0.95 0.02 240)` | `#E3F1FB` | 1.32 / 1.10 |
| `--offline` | `oklch(0.70 0.01 260)` | `#9B9FA5` | `oklch(0.50 0.012 260)` | `#5F636A` | 6.71 / 5.70 |

## Charts

| Token | Dark (OKLCH) | Dark HEX | Light (OKLCH) | Light HEX | Contrast dark / light |
|---|---|---|---|---|---|
| `--chart-1` | `oklch(0.72 0.11 255)` | `#74A7E8` | `oklch(0.55 0.12 255)` | `#3D73B6` | 7.23 / 4.62 |
| `--chart-2` | `oklch(0.74 0.11 320)` | `#CA94D6` | `oklch(0.56 0.13 320)` | `#9558A3` | 7.42 / 4.71 |
| `--chart-3` | `oklch(0.78 0.11 75)` | `#E1AD63` | `oklch(0.62 0.12 70)` | `#B47825` | 8.83 / 3.54 |
| `--chart-4` | `oklch(0.74 0.10 185)` | `#55BFB3` | `oklch(0.57 0.09 185)` | `#25887E` | 8.12 / 4.05 |
| `--chart-5` | `oklch(0.72 0.12 30)` | `#E68677` | `oklch(0.56 0.14 30)` | `#B84F40` | 6.88 / 4.72 |
| `--chart-6` | `oklch(0.72 0.10 290)` | `#A39ADF` | `oklch(0.55 0.11 290)` | `#7165AD` | 7.03 / 4.78 |
| `--chart-grid` | `oklch(0.32 0.008 260)` | `#303337` | `oklch(0.92 0.004 85)` | `#E6E4E2` | 1.41 / 1.20 |

## Accent ramp

| Step | OKLCH | HEX |
|---|---|---|
| `--accent-50` | `oklch(0.97 0.012 255)` | `#F0F6FD` |
| `--accent-100` | `oklch(0.94 0.026 255)` | `#E0ECFD` |
| `--accent-200` | `oklch(0.88 0.05 255)` | `#C2DAF9` |
| `--accent-300` | `oklch(0.80 0.075 255)` | `#9DC1EE` |
| `--accent-400` | `oklch(0.72 0.10 255)` | `#79A7E2` |
| `--accent-500` | `oklch(0.63 0.12 255)` | `#548BD0` |
| `--accent-600` | `oklch(0.54 0.125 255)` | `#3770B5` |
| `--accent-700` | `oklch(0.47 0.115 255)` | `#285B9A` |
| `--accent-800` | `oklch(0.39 0.095 255)` | `#1D4576` |
| `--accent-900` | `oklch(0.31 0.075 255)` | `#133155` |

## Component anatomy

- Control height 36 px (44 px touch on phones), radius 8 px, 1 px `--stroke-strong` border, focus = 2 px `--focus` outline.
- Buttons: primary (accent, one per view), secondary (outlined), ghost, danger (soft red), danger-solid (confirm dialogs only).
- Badges: icon + words + colour (never colour alone); neutral uses a dot.
- Cards: `--bg-surface` + `--stroke-subtle`, 12 px radius, no shadow; only popovers/dialogs/sheets (`--bg-raised`) get `--shadow-raised`.
- States: hover = one surface step, pressed = accent-pressed / brightness, focus-visible outline, disabled 55 % + explanation, pending = spinner + verb ("Applying…").
