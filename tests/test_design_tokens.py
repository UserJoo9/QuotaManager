"""Design tokens (web/css/tokens.css): WCAG 2.2 AA contrast is computed from the
real OKLCH values for both themes, not assumed from how the palette looks."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

TOKENS = Path(__file__).resolve().parent.parent / "web" / "css" / "tokens.css"


def _blocks() -> dict[str, dict[str, str]]:
    css = TOKENS.read_text(encoding="utf-8")
    shared = dict(re.findall(r"--([\w-]+):\s*([^;]+);", css[:css.index("/* @theme dark */")]))
    out = {}
    for theme in ("dark", "light"):
        start = css.index(f"/* @theme {theme} */")
        body = css[css.index("{", start) + 1:css.index("}", start)]
        out[theme] = {**shared, **dict(re.findall(r"--([\w-]+):\s*([^;]+);", body))}
    return out


def _resolve(v: dict[str, str], name: str) -> str:
    val = v[name].strip()
    for _ in range(5):
        m = re.fullmatch(r"var\(--([\w-]+)\)", val)
        if not m:
            return val
        val = v[m.group(1)].strip()
    return val


def _oklch_to_srgb(s: str) -> tuple[float, float, float]:
    m = re.fullmatch(r"oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*(?:/\s*[\d.]+\s*)?\)", s)
    assert m, f"not an oklch() colour: {s}"
    L, C, H = (float(x) for x in m.groups())
    a, b = C * math.cos(math.radians(H)), C * math.sin(math.radians(H))
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    bl = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    return tuple(min(1.0, max(0.0, x)) for x in (r, g, bl))  # linear sRGB, gamut-clipped


def _lum(s: str) -> float:
    r, g, b = _oklch_to_srgb(s)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(c1: str, c2: str) -> float:
    a, b = sorted((_lum(c1), _lum(c2)), reverse=True)
    return (a + 0.05) / (b + 0.05)


THEMES = _blocks()
BACKGROUNDS = ("bg-canvas", "bg-surface", "bg-raised", "bg-frame")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_four_surfaces_two_strokes_three_text_tiers(theme):
    v = THEMES[theme]
    for k in ("bg-frame", "bg-canvas", "bg-surface", "bg-raised", "stroke-subtle", "stroke-strong",
              "text-primary", "text-secondary", "text-tertiary"):
        assert k in v, f"{theme}: missing --{k}"
    assert all(f"accent-{n}" in v for n in (50, 100, 200, 300, 400, 500, 600, 700, 800, 900))
    # no pure black/white text (softened charcoal / near-white)
    assert _resolve(v, "text-primary") not in ("oklch(1 0 0)", "oklch(0 0 0)")


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("text", ["text-primary", "text-secondary", "text-tertiary"])
def test_body_text_is_aa_on_every_surface(theme, text):
    v = THEMES[theme]
    for bg in BACKGROUNDS:
        r = contrast(_resolve(v, text), _resolve(v, bg))
        assert r >= 4.5, f"{theme}: --{text} on --{bg} = {r:.2f}:1 (< 4.5)"


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("state", ["success", "warning", "danger", "info", "accent-fg"])
def test_state_text_is_aa_on_surfaces_and_its_own_soft_chip(theme, state):
    v = THEMES[theme]
    soft = {"accent-fg": "accent-soft"}.get(state, f"{state}-soft")
    for bg in ("bg-canvas", "bg-surface", soft):
        r = contrast(_resolve(v, state), _resolve(v, bg))
        assert r >= 4.5, f"{theme}: --{state} on --{bg} = {r:.2f}:1"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_primary_button_text_and_destructive_button(theme):
    v = THEMES[theme]
    assert contrast(_resolve(v, "accent-ink"), _resolve(v, "accent")) >= 4.5
    assert contrast("oklch(1 0 0)", _resolve(v, "danger-solid")) >= 4.5


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_non_text_contrast_focus_inputs_and_charts(theme):
    """WCAG 1.4.11: focus ring, input borders and chart marks need 3:1 against what they sit on."""
    v = THEMES[theme]
    for bg in ("bg-canvas", "bg-surface", "bg-raised"):
        assert contrast(_resolve(v, "focus"), _resolve(v, bg)) >= 3, f"{theme}: focus on {bg}"
        assert contrast(_resolve(v, "stroke-strong"), _resolve(v, bg)) >= 3, f"{theme}: input border on {bg}"
        for i in (1, 2, 3, 4, 5, 6):
            assert contrast(_resolve(v, f"chart-{i}"), _resolve(v, bg)) >= 3, f"{theme}: chart-{i} on {bg}"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_chart_series_are_told_apart_without_hue(theme):
    """Download/upload are also drawn solid vs dashed; their lightness must not be identical either."""
    v = THEMES[theme]
    l1, l2 = _lum(_resolve(v, "chart-1")), _lum(_resolve(v, "chart-2"))
    assert l1 != l2


def test_surface_ladder_rises_in_dark_mode():
    """Dark elevation = lighter (a popover must never sink below the page)."""
    v = THEMES["dark"]
    order = [_lum(_resolve(v, k)) for k in ("bg-frame", "bg-canvas", "bg-surface", "bg-raised")]
    assert order == sorted(order) and len(set(order)) == 4


def test_no_decorative_background_effects_left():
    """The animated blobs/grid/particles cost ~7 % idle CPU on the gateway laptop; gone for good."""
    web = TOKENS.parent.parent
    css = "".join(p.read_text(encoding="utf-8") for p in web.rglob("*.css"))
    assert ".blob" not in css and "@keyframes drift" not in css and "#bg-particles" not in css
    assert not list(web.rglob("particles*.js"))
    src = TOKENS.parent.parent.parent / "frontend" / "src"
    assert not any("backdrop-blur" in p.read_text(encoding="utf-8") for p in src.rglob("*.tsx"))
