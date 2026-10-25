/* Apply the dashboard's per-browser look (theme + accent) to the public pages. */
(function () {
  var A = {azure:["#5B8CFF","#06101F","#5EEAD4","#3B63F0","#FFFFFF","#0EA5C6"],violet:["#A58BFF","#0E0820","#F0A6FF","#6D4AE0","#FFFFFF","#C04AE0"],
           emerald:["#34D6A0","#03130C","#B7F34D","#0A7F58","#FFFFFF","#5BAA12"],coral:["#FF8A73","#1F0904","#FFC857","#C93A25","#FFFFFF","#E58A00"],
           amber:["#F5B544","#1A1000","#FFE08A","#8A5A00","#FFFFFF","#C98A00"]};
  var p = {};
  try { p = JSON.parse(localStorage.getItem("qm.ui.prefs.v2") || "{}") || {}; } catch (e) {}
  var th = p.theme === "light" ? "light" : p.theme === "auto" ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : "dark";
  try { var own = localStorage.getItem("qm.share.theme"); if (own === "light" || own === "dark") th = own; } catch (e) {}
  var r = document.documentElement, c = A[p.accent] || A.azure, o = th === "light" ? 3 : 0;
  r.dataset.theme = th;
  r.style.setProperty("--accent", c[o]); r.style.setProperty("--accent-ink", c[o + 1]); r.style.setProperty("--accent-2", c[o + 2]);
})();
