// Dependency-free SVG charts in the prototype's look: dashed grid, gradient area + glow line, HTML axis labels.
const NS = "http://www.w3.org/2000/svg";
const el = (n, a = {}) => { const e = document.createElementNS(NS, n); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); return e; };
const W = 600, H = 190;
const yOf = (v, max) => 6 + (1 - Math.min(1, v / max)) * (H - 12);

function base(id) {
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  const defs = el("defs");
  const g = el("linearGradient", { id: "aGrad" + id, x1: 0, y1: 0, x2: 0, y2: 1 });
  g.append(el("stop", { offset: 0, style: "stop-color:var(--accent);stop-opacity:.32" }), el("stop", { offset: 1, style: "stop-color:var(--accent);stop-opacity:0" }));
  const b = el("linearGradient", { id: "barGrad", x1: 0, y1: 0, x2: 0, y2: 1 });
  b.append(el("stop", { offset: 0, style: "stop-color:var(--accent)" }), el("stop", { offset: 1, style: "stop-color:var(--accent-2)" }));
  defs.append(g, b); svg.append(defs);
  const grid = el("g", { class: "grid-l" });
  for (const y of [1, 95, 189]) grid.append(el("line", { x1: 0, x2: W, y1: y, y2: y }));
  svg.append(grid);
  return svg;
}
const smooth = (pts) => {
  if (pts.length < 2) return "";
  let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], cx = (x0 + x1) / 2;
    d += `C${cx.toFixed(1)} ${y0.toFixed(1)} ${cx.toFixed(1)} ${y1.toFixed(1)} ${x1.toFixed(1)} ${y1.toFixed(1)}`;
  }
  return d;
};
// two series (down = accent with area, up = accent-2 line). Returns {svg}
export function lineChart(down, up, max) {
  const svg = base("L");
  if (down.length === 1) { down = [down[0], down[0]]; up = [up[0], up[0]]; }
  const n = down.length;
  if (n >= 2) {
    const step = W / (n - 1);
    const pd = down.map((v, i) => [i * step, yOf(v, max)]), pu = up.map((v, i) => [i * step, yOf(v, max)]);
    const line = smooth(pd);
    svg.append(el("path", { class: "a-down", style: "fill:url(#aGradL)", d: `${line}L${W} ${H}L0 ${H}Z` }),
      el("path", { class: "l-down", d: line }), el("path", { class: "l-up", d: smooth(pu) }));
  }
  return svg;
}
// bars: [{value,title,today}]
export function barChart(bars, max) {
  const svg = base("B");
  const n = bars.length, gap = 4, bw = (W - gap * (n - 1)) / n;
  bars.forEach((b, i) => {
    const hh = Math.max(b.value > 0 ? 3 : 0, (b.value / max) * (H - 12));
    const r = el("rect", { class: "cbar" + (b.today ? " today" : ""), x: (i * (bw + gap)).toFixed(1), y: (H - 1 - hh).toFixed(1), width: bw.toFixed(1), height: hh.toFixed(1), rx: 3 });
    const tt = el("title"); tt.textContent = b.title; r.append(tt); svg.append(r);
  });
  return svg;
}
// semi-circular-ish gauge ring (270deg) - returns svg element
export function gauge(pct, forecastPct) {
  const R = 80, C = 2 * Math.PI * R, arc = C * 0.75;
  const clamp = (x) => Math.max(0, Math.min(100, x));
  const svg = el("svg", { class: "g-svg", viewBox: "0 0 200 200", "aria-hidden": "true" });
  const defs = el("defs"); const lg = el("linearGradient", { id: "gGrad", x1: 0, y1: 0, x2: 1, y2: 1 });
  lg.append(el("stop", { offset: 0, style: "stop-color:var(--accent)" }), el("stop", { offset: 1, style: "stop-color:var(--accent-2)" })); defs.append(lg); svg.append(defs);
  const g = el("g", { transform: "rotate(135 100 100)" });
  const circ = (cls, len, extra = {}) => el("circle", { class: cls, cx: 100, cy: 100, r: R, style: `stroke-dasharray:${len} ${C};` + (extra.style || "") });
  g.append(circ("g-track", arc));
  if (forecastPct != null) g.append(circ("g-fc", arc * clamp(forecastPct) / 100));
  const over = pct >= 100;
  g.append(circ("g-prog" + (over ? " over" : ""), arc * clamp(pct) / 100, { style: over ? "" : "stroke:url(#gGrad);" }));
  svg.append(g); return svg;
}
