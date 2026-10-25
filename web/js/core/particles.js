// Ambient background particles (ported from the classic dashboard). Honors the "particles" pref,
// the effects level and prefers-reduced-motion; pauses while the tab is hidden.
import { getPrefs, TONES, ACCENTS, effTheme } from "./prefs.js";

let started = false;
export function startParticles() {
  if (started) return; started = true;
  const canvas = document.createElement("canvas");
  canvas.id = "bg-particles"; canvas.setAttribute("aria-hidden", "true");
  document.body.prepend(canvas);
  const c = canvas.getContext("2d"); if (!c) return;
  let w = 0, hh = 0, raf = 0, ps = [], style = "off", rgb = "91,140,255";
  const reduced = () => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch { return false; } };
  const hex = (x) => { const n = parseInt(x.slice(1), 16); return `${n >> 16},${(n >> 8) & 255},${n & 255}`; };
  const resize = () => { const d = Math.min(devicePixelRatio || 1, 2); w = innerWidth; hh = innerHeight; canvas.width = w * d; canvas.height = hh * d; c.setTransform(d, 0, 0, d, 0, 0); };
  const R = Math.random;
  function seed() {
    ps = []; if (style === "off") return;
    if (style === "rain") for (let i = 0; i < Math.floor(w / 35); i++) ps.push({ x: R() * w, y: R() * hh, len: R() * 24 + 14, sp: R() * 3.5 + 2.5, a: R() * .35 + .25 });
    else if (style === "orbs") for (let i = 0; i < 18; i++) ps.push({ x: R() * w, y: R() * hh, r: R() * 28 + 14, vx: (R() - .5) * .35, vy: (R() - .5) * .35, a: R() * .08 + .05, tw: R() * 6.28 });
    else for (let i = 0; i < (style === "mesh" ? 42 : 45); i++) ps.push({ x: R() * w, y: R() * hh, r: style === "mesh" ? R() * 1.4 + 1.2 : R() * 2 + 1.2, vx: (R() - .5) * .5, vy: style === "dust" ? -(R() * .5 + .3) : (R() - .5) * .45, a: R() * .2 + .35, tw: R() * 6.28 });
  }
  function tick() {
    c.clearRect(0, 0, w, hh);
    if (style === "rain") {
      for (const p of ps) {
        p.y += p.sp; if (p.y > hh + 30) { p.y = -30; p.x = R() * w; }
        const g = c.createLinearGradient(p.x, p.y - p.len, p.x, p.y); g.addColorStop(0, `rgba(${rgb},0)`); g.addColorStop(1, `rgba(${rgb},${p.a.toFixed(3)})`);
        c.strokeStyle = g; c.lineWidth = 1.6; c.beginPath(); c.moveTo(p.x, p.y - p.len); c.lineTo(p.x, p.y); c.stroke();
        c.fillStyle = `rgba(${rgb},${(p.a * 1.3).toFixed(3)})`; c.beginPath(); c.arc(p.x, p.y, 1.2, 0, 6.283); c.fill();
      }
    } else if (style === "orbs") {
      for (const p of ps) {
        p.x += p.vx; p.y += p.vy; p.tw += .01;
        if (p.x < -p.r) p.x = w + p.r; if (p.x > w + p.r) p.x = -p.r; if (p.y < -p.r) p.y = hh + p.r; if (p.y > hh + p.r) p.y = -p.r;
        const g = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r); g.addColorStop(0, `rgba(${rgb},${(p.a * (.8 + .2 * Math.sin(p.tw))).toFixed(3)})`); g.addColorStop(1, `rgba(${rgb},0)`);
        c.fillStyle = g; c.beginPath(); c.arc(p.x, p.y, p.r, 0, 6.283); c.fill();
      }
    } else {
      c.shadowColor = `rgba(${rgb},.7)`; c.shadowBlur = 8;
      for (let i = 0; i < ps.length; i++) {
        const p = ps[i]; p.x += p.vx; p.y += p.vy; p.tw += .01;
        if (p.y < -8) { p.y = hh + 8; p.x = R() * w; } if (p.y > hh + 8) { p.y = -8; p.x = R() * w; } if (p.x < -8) p.x = w + 8; if (p.x > w + 8) p.x = -8;
        c.beginPath(); c.arc(p.x, p.y, p.r, 0, 6.283); c.fillStyle = `rgba(${rgb},${(p.a * (.7 + .3 * Math.sin(p.tw))).toFixed(3)})`; c.fill();
        if (style === "mesh") for (let j = i + 1; j < ps.length; j++) {
          const q = ps[j], d = Math.hypot(p.x - q.x, p.y - q.y);
          if (d < 130) { c.beginPath(); c.moveTo(p.x, p.y); c.lineTo(q.x, q.y); c.strokeStyle = `rgba(${rgb},${((1 - d / 130) * .18).toFixed(3)})`; c.lineWidth = .9; c.stroke(); }
        }
      }
      c.shadowBlur = 0;
    }
    raf = requestAnimationFrame(tick);
  }
  const stop = () => { if (raf) cancelAnimationFrame(raf); raf = 0; };
  function sync() {
    const p = getPrefs(), th = effTheme(), tn = p.tone && TONES[p.tone];
    rgb = hex(tn ? (th === "dark" ? tn.a : tn.la) : ACCENTS[p.accent][th].a);
    const next = reduced() || p.fx === "off" ? "off" : p.particles;
    const changed = next !== style; style = next;
    stop(); c.clearRect(0, 0, w, hh);
    if (style === "off") return;
    if (changed || !ps.length) seed();
    if (!document.hidden) raf = requestAnimationFrame(tick);
  }
  resize(); sync();
  addEventListener("resize", () => { resize(); seed(); });
  document.addEventListener("visibilitychange", () => { document.hidden ? stop() : sync(); });
  document.documentElement.addEventListener("qm:prefs", sync);
}
