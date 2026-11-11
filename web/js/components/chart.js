// Dependency-free SVG charts in the prototype's look: dashed grid, gradient area + glow line, HTML axis labels.
/**
 * @fileoverview Dependency-free SVG charts: two-series line chart (area + glow line), bar chart,
 * and a 270° gauge ring. Geometry lives in the viewBox; colors come from CSS tokens (--accent, --accent-2).
 * One <svg> per call keeps gradient ids scoped to the element that uses them.
 */
const NS = "http://www.w3.org/2000/svg";
/** Create an element in the SVG namespace and set attributes from a map. */
const el = (n, a = {}) => { const e = document.createElementNS(NS, n); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); return e; };

// ViewBox geometry (units). Named constants instead of magic numbers.
const CHART_W = 600, CHART_H = 190;
const CHART_PAD = 6;                    // padding above/below the plot area
const CHART_GRID_Y = [1, 95, 189];      // dashed grid line positions
const BAR_GAP = 4, BAR_MIN_H = 3, BAR_RX = 3;
const GAUGE_R = 80, GAUGE_C = 100;      // gauge ring radius + center
const GAUGE_ROT = "rotate(135 100 100)";
const GAUGE_ARC_FRAC = 0.75;            // 270° of the ring carries the value

/** Vertical pixel for value `v` out of `max` (`v` clamped to [0, max]). */
const yOf = (v, max) => CHART_PAD + (1 - Math.min(1, v / max)) * (CHART_H - 2 * CHART_PAD);

/** SVG shell: viewBox, gradients, dashed grid. Returns the <svg> element. */
function base(id) {
  const svg = el("svg", { viewBox: `0 0 ${CHART_W} ${CHART_H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  const defs = el("defs");
  const g = el("linearGradient", { id: `aGrad${id}`, x1: 0, y1: 0, x2: 0, y2: 1 });
  g.append(el("stop", { offset: 0, style: "stop-color:var(--accent);stop-opacity:.32" }), el("stop", { offset: 1, style: "stop-color:var(--accent);stop-opacity:0" }));
  const b = el("linearGradient", { id: "barGrad", x1: 0, y1: 0, x2: 0, y2: 1 });
  b.append(el("stop", { offset: 0, style: "stop-color:var(--accent)" }), el("stop", { offset: 1, style: "stop-color:var(--accent-2)" }));
  defs.append(g, b); svg.append(defs);
  const grid = el("g", { class: "grid-l" });
  for (const y of CHART_GRID_Y) grid.append(el("line", { x1: 0, x2: CHART_W, y1: y, y2: y }));
  svg.append(grid);
  return svg;
}

/** Cubic Bézier through `pts`; control points sit at the mid-x of each segment. */
const smooth = (pts) => {
  if (pts.length < 2) return "";
  let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], cx = (x0 + x1) / 2;
    d += `C${cx.toFixed(1)} ${y0.toFixed(1)} ${cx.toFixed(1)} ${y1.toFixed(1)} ${x1.toFixed(1)} ${y1.toFixed(1)}`;
  }
  return d;
};

/**
 * Two series: `down` (accent area below the line) and `up` (accent-2 line).
 * A single point is duplicated so the line still paints.
 * @param {number[]} down - values for the area series
 * @param {number[]} up - values for the line series
 * @param {number} max - scale top
 * @returns {SVGSVGElement}
 */
export function lineChart(down, up, max) {
  const svg = base("L");
  if (down.length === 1) { down = [down[0], down[0]]; up = [up[0], up[0]]; }
  const n = down.length;
  if (n >= 2) {
    const step = CHART_W / (n - 1);
    const pd = down.map((v, i) => [i * step, yOf(v, max)]), pu = up.map((v, i) => [i * step, yOf(v, max)]);
    const line = smooth(pd);
    svg.append(el("path", { class: "a-down", style: "fill:url(#aGradL)", d: `${line}L${CHART_W} ${CHART_H}L0 ${CHART_H}Z` }),
      el("path", { class: "l-down", d: line }), el("path", { class: "l-up", d: smooth(pu) })));
  }
  return svg;
}
/**
 * Bars: [{value, title, today?}]. `today` bars get the accent-2 fill.
 * @param {{value:number, title:string, today?:boolean}[]} bars
 * @param {number} max - scale top
 * @returns {SVGSVGElement}
 */
export function barChart(bars, max) {
  const svg = base("B");
  const n = bars.length, gap = BAR_GAP, bw = (CHART_W - gap * (n - 1)) / n;
  bars.forEach((b, i) => {
    const hh = Math.max(b.value > 0 ? BAR_MIN_H : 0, (b.value / max) * (CHART_H - 12));
    const r = el("rect", { class: "cbar" + (b.today ? " today" : ""), x: (i * (bw + gap)).toFixed(1), y: (CHART_H - 1 - hh).toFixed(1), width: bw.toFixed(1), height: hh.toFixed(1), rx: BAR_RX });
    const tt = el("title"); tt.textContent = b.title; r.append(tt); svg.append(r);
  });
  return svg;
}
/**
 * Semi-circular gauge ring (270°). `forecastPct`, when given, draws a secondary track.
 * @param {number} pct - fill percent (clamped to [0, 100], >= 100 paints "over")
 * @param {number|null|undefined} forecastPct - secondary track percent
 * @returns {SVGSVGElement}
 */
export function gauge(pct, forecastPct) {
  const R = GAUGE_R, C = 2 * Math.PI * R, arc = C * GAUGE_ARC_FRAC;
  const clamp = (x) => Math.max(0, Math.min(100, x));
  const svg = el("svg", { class: "g-svg", viewBox: "0 0 200 200", "aria-hidden": "true" });
  const defs = el("defs"); const lg = el("linearGradient", { id: "gGrad", x1: 0, y1: 0, x2: 1, y2: 1 });
  lg.append(el("stop", { offset: 0, style: "stop-color:var(--accent)" }), el("stop", { offset: 1, style: "stop-color:var(--accent-2)" })); defs.append(lg); svg.append(defs);
  const g = el("g", { transform: GAUGE_ROT });
  const circ = (cls, len, extra = {}) => el("circle", { class: cls, cx: GAUGE_C, cy: GAUGE_C, r: R, style: `stroke-dasharray:${len} ${C};` + (extra.style || "") });
  g.append(circ("g-track", arc));
  if (forecastPct != null) g.append(circ("g-fc", arc * clamp(forecastPct) / 100));
  const over = pct >= 100;
  g.append(circ("g-prog" + (over ? " over" : ""), arc * clamp(pct) / 100, { style: over ? "" : "stroke:url(#gGrad);" }));
  svg.append(g); return svg;
}
