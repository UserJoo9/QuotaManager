/* Tiny Arabic/English switch for the public pages (/share, /milestone).
 * Source strings are the English texts themselves; a page registers its Arabic
 * dictionary with QMI.add({...}). Static markup is translated in place by
 * matching text nodes / title / placeholder / aria-label; dynamic strings go
 * through QMI.t("text with {name}", {name: value}). The choice is kept per
 * browser (qm.pub.lang) and defaults to the dashboard's language. */
(function () {
  var ar = {}, lang = "en", subs = [], store = new WeakMap(), btn = null;
  var ATTRS = ["title", "placeholder", "aria-label"];

  function pick() {
    try { var l = localStorage.getItem("qm.pub.lang"); if (l === "ar" || l === "en") return l; } catch (e) {}
    try { var p = JSON.parse(localStorage.getItem("qm.ui.prefs.v2") || "{}"); if (p.lang === "ar" || p.lang === "en") return p.lang; } catch (e) {}
    return (navigator.language || "en").toLowerCase().indexOf("ar") === 0 ? "ar" : "en";
  }
  function norm(s) { return String(s).replace(/\s+/g, " ").trim(); }
  function t(en, params) {
    var s = (lang === "ar" && ar[en]) || en;
    if (params) s = s.replace(/\{(\w+)\}/g, function (m, k) { return params[k] == null ? m : params[k]; });
    return s;
  }
  function apply(root) {
    root = root || document.body;
    if (!root) return;
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), n, list = [];
    while ((n = w.nextNode())) list.push(n);
    list.forEach(function (n) {
      var p = n.parentNode && n.parentNode.nodeName;
      if (p === "SCRIPT" || p === "STYLE") return;
      var o = store.get(n);
      if (!o) {
        var k = norm(n.nodeValue);
        if (!k || !ar[k]) return;
        o = { key: k, lead: (n.nodeValue.match(/^\s*/) || [""])[0], trail: (n.nodeValue.match(/\s*$/) || [""])[0] };
        store.set(n, o);
      }
      n.nodeValue = o.lead + t(o.key) + o.trail;
    });
    var els = (root.querySelectorAll ? root.querySelectorAll("[title],[placeholder],[aria-label]") : []);
    Array.prototype.forEach.call(els, function (el) { fixAttrs(el); });
    if (root.getAttribute) fixAttrs(root);
  }
  function fixAttrs(el) {
    ATTRS.forEach(function (a) {
      if (!el.hasAttribute(a)) return;
      var key = "qmi" + a.replace(/-/g, "_"), o = el.dataset[key];
      if (o === undefined) { if (!ar[norm(el.getAttribute(a))]) return; o = el.getAttribute(a); el.dataset[key] = o; }
      el.setAttribute(a, t(norm(o)));
    });
  }
  function paintBtn() { if (btn) { btn.textContent = lang === "ar" ? "EN" : "عربي"; btn.title = lang === "ar" ? "English" : "العربية"; btn.setAttribute("aria-label", btn.title); } }
  function set(l, keep) {
    lang = l === "ar" ? "ar" : "en";
    if (!keep) { try { localStorage.setItem("qm.pub.lang", lang); } catch (e) {} }
    var r = document.documentElement;
    r.lang = lang; r.dir = lang === "ar" ? "rtl" : "ltr";
    if (!document.documentElement.dataset.qmiTitle) document.documentElement.dataset.qmiTitle = document.title;
    document.title = t(document.documentElement.dataset.qmiTitle);
    apply(document.body); paintBtn();
    subs.forEach(function (f) { try { f(lang); } catch (e) {} });
  }
  /* A small language button; `parent` receives it, `cls` styles it. */
  function mount(parent, cls) {
    if (!parent || btn) return;
    btn = document.createElement("button");
    btn.type = "button"; btn.className = cls || "btn-icon"; btn.id = "lang-btn";
    btn.style.fontWeight = "700"; btn.style.minWidth = "40px";
    btn.addEventListener("click", function () { set(lang === "ar" ? "en" : "ar"); });
    parent.insertBefore(btn, parent.firstChild);
    paintBtn();
  }
  lang = pick();
  window.QMI = {
    t: t, apply: apply, set: set, mount: mount,
    get lang() { return lang; },
    add: function (d) { for (var k in d) ar[k] = d[k]; },
    onChange: function (f) { subs.push(f); }
  };
  function boot() { set(lang, true); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
