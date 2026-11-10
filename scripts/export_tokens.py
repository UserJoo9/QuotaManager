#!/usr/bin/env python3
"""Export web/css/tokens.css (OKLCH, the source of truth) to docs/redesign-0.6/:
tokens.json (OKLCH + HEX per theme, for design tools) and DESIGN-TOKENS.md (table).
Run after changing tokens.css (dev venv, it reuses the contrast maths of the tests):
    .venv/bin/python scripts/export_tokens.py"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_design_tokens import THEMES, _resolve, _oklch_to_srgb, contrast  # noqa: E402

GROUPS = {
    "Surfaces": ["bg-frame", "bg-canvas", "bg-surface", "bg-raised"],
    "Strokes": ["stroke-subtle", "stroke-strong"],
    "Text": ["text-primary", "text-secondary", "text-tertiary"],
    "Accent (functional)": ["accent", "accent-hover", "accent-pressed", "accent-ink", "accent-soft", "accent-fg", "focus"],
    "Semantic": ["success", "success-soft", "warning", "warning-soft", "danger", "danger-soft", "danger-solid", "info", "info-soft", "offline"],
    "Charts": [f"chart-{i}" for i in range(1, 7)] + ["chart-grid"],
}
RAMP = [f"accent-{n}" for n in (50, 100, 200, 300, 400, 500, 600, 700, 800, 900)]


def hexify(oklch: str) -> str:
    def enc(c: float) -> int:
        c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        return round(max(0.0, min(1.0, c)) * 255)
    r, g, b = _oklch_to_srgb(oklch)
    return "#%02X%02X%02X" % (enc(r), enc(g), enc(b))


def main() -> None:
    out_dir = ROOT / "docs" / "redesign-0.6"
    out_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"source": "web/css/tokens.css", "note": "OKLCH is canonical; HEX is a compatibility export.", "ramp": {}, "themes": {}}
    for k in RAMP:
        v = _resolve(THEMES["dark"], k)
        data["ramp"][k] = {"oklch": v, "hex": hexify(v)}
    for theme, v in THEMES.items():
        data["themes"][theme] = {g: {k: {"oklch": _resolve(v, k), "hex": hexify(_resolve(v, k))} for k in ks} for g, ks in GROUPS.items()}
    (out_dir / "tokens.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    md = ["# Design tokens (generated)", "", "Source of truth: `web/css/tokens.css` (OKLCH). Regenerate with",
          "`.venv/bin/python scripts/export_tokens.py`. HEX values are a compatibility export for design tools.", "",
          "Contrast is against `--bg-canvas` of the same theme (WCAG 2.x ratio).", ""]
    for g, ks in GROUPS.items():
        md += [f"## {g}", "", "| Token | Dark (OKLCH) | Dark HEX | Light (OKLCH) | Light HEX | Contrast dark / light |", "|---|---|---|---|---|---|"]
        for k in ks:
            d, l_ = _resolve(THEMES["dark"], k), _resolve(THEMES["light"], k)
            cd, cl = contrast(d, _resolve(THEMES["dark"], "bg-canvas")), contrast(l_, _resolve(THEMES["light"], "bg-canvas"))
            md.append(f"| `--{k}` | `{d}` | `{hexify(d)}` | `{l_}` | `{hexify(l_)}` | {cd:.2f} / {cl:.2f} |")
        md.append("")
    md += ["## Accent ramp", "", "| Step | OKLCH | HEX |", "|---|---|---|"] + [f"| `--{k}` | `{data['ramp'][k]['oklch']}` | `{data['ramp'][k]['hex']}` |" for k in RAMP] + [""]
    md += ["## Component anatomy", "", "- Control height 36 px (44 px touch on phones), radius 8 px, 1 px `--stroke-strong` border, focus = 2 px `--focus` outline.",
           "- Buttons: primary (accent, one per view), secondary (outlined), ghost, danger (soft red), danger-solid (confirm dialogs only).",
           "- Badges: icon + words + colour (never colour alone); neutral uses a dot.",
           "- Cards: `--bg-surface` + `--stroke-subtle`, 12 px radius, no shadow; only popovers/dialogs/sheets (`--bg-raised`) get `--shadow-raised`.",
           "- States: hover = one surface step, pressed = accent-pressed / brightness, focus-visible outline, disabled 55 % + explanation, pending = spinner + verb (\"Applying…\").", ""]
    (out_dir / "DESIGN-TOKENS.md").write_text("\n".join(md), encoding="utf-8")
    print("wrote", out_dir / "tokens.json", "and DESIGN-TOKENS.md")


if __name__ == "__main__":
    main()
