import { h, icon } from "../core/dom.js";
import { t, num, iso, getLang } from "../core/i18n.js";
import { api } from "../core/api.js";
import { sw, chip } from "../components/ui.js";
import { openDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
const tr = (k, fb) => { const v = t(k); return v === k ? fb : v; };

const RULE_PAGE_SIZE = 50;
const dns = { presets: null, blocklists: null, selectedFeeds: new Set(), rules: [], ruleTotal: 0, ruleOffset: 0, ruleQuery: "", ruleRequest: 0, rulesLoading: false, rulesError: false, searchTimer: null, loading: false, at: 0, failAt: 0, busy: new Set(), tier: "adblock-ultra", feedName: "", feedUrl: "", feedResult: "", feedResultError: false, armedFeed: null, imp: { text: "", format: "auto", scope: "global", target: "", action: "block", result: "" } };
const TIERS = ["adblock-ultra", "ads-tracking"];

function syncFeedSelectionView() {
  const feeds = dns.blocklists || [];
  const selectedCount = feeds.filter((feed) => dns.selectedFeeds.has(Number(feed.id))).length;
  const bulkBusy = dns.busy.has("feed:bulk");
  const selectAll = document.querySelector(".dns-feed-select-all");
  if (selectAll) {
    selectAll.checked = feeds.length > 0 && selectedCount === feeds.length;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < feeds.length;
    selectAll.disabled = bulkBusy;
  }
  document.querySelectorAll(".dns-feed-select").forEach((input) => {
    input.checked = dns.selectedFeeds.has(Number(input.value));
    input.disabled = bulkBusy;
  });
  const removeSelected = document.querySelector(".dns-feed-remove-selected");
  if (removeSelected) {
    removeSelected.disabled = bulkBusy || selectedCount === 0;
    removeSelected.textContent = t("dns.feed.removeSelected")
      .replace("{count}", num(selectedCount, 0));
  }
  const removeAll = document.querySelector(".dns-feed-remove-all");
  if (removeAll) {
    removeAll.disabled = bulkBusy || feeds.length === 0;
    removeAll.textContent = t("dns.feed.removeAll")
      .replace("{count}", num(feeds.length, 0));
  }
}

function confirmBulkFeedRemoval(ctx, requestedIds, removeAll) {
  const available = new Set((dns.blocklists || []).map((feed) => Number(feed.id)));
  const feedIds = [...new Set(requestedIds.map(Number))]
    .filter((id) => available.has(id));
  if (!feedIds.length || dns.busy.has("feed:bulk")) return;

  const count = num(feedIds.length, 0);
  const descriptionKey = removeAll
    ? "dns.feed.bulkRemove.all" : "dns.feed.bulkRemove.selected";
  const acknowledgement = h("input", { type: "checkbox", required: true });
  openDialog({
    title: t("dns.feed.bulkRemove.title"),
    sub: t(descriptionKey).replace("{count}", count),
    submitLabel: t("dns.feed.bulkRemove.confirm"),
    content: [h("label", { class: "dns-feed-bulk-confirm" },
      acknowledgement, h("span", {}, t("dns.feed.bulkRemove.confirmText")))],
    onSubmit: async () => {
      dns.busy.add("feed:bulk");
      let removed = 0;
      let failed = 0;
      try {
        for (const feedId of feedIds) {
          try {
            await api.del(`/api/dns/blocklists/${feedId}`);
            dns.selectedFeeds.delete(feedId);
            removed += 1;
          } catch {
            failed += 1;
          }
        }
        if (failed === 0) {
          dns.feedResult = t("dns.feed.bulkRemove.success")
            .replace("{count}", num(removed, 0));
          dns.feedResultError = false;
        } else if (removed > 0) {
          dns.feedResult = t("dns.feed.bulkRemove.partial")
            .replace("{removed}", num(removed, 0))
            .replace("{failed}", num(failed, 0));
          dns.feedResultError = true;
        } else {
          dns.feedResult = t("dns.feed.bulkRemove.failed");
          dns.feedResultError = true;
        }
      } finally {
        dns.busy.delete("feed:bulk");
        await load(ctx, true);
      }
      return true;
    },
  });
}

function renderRuleResults(ctx) {
  if (dns.rulesLoading) return h("div", { class: "skeleton", style: { height: "180px" }, "aria-label": t("dns.rules.loading") });
  if (dns.rulesError) return h("div", { class: "empty", role: "alert" }, t("dns.rules.error"), h("button", { type: "button", class: "btn ghost sm", onclick: () => loadRules(ctx) }, t("dns.rules.retry")));
  if (!dns.rules.length) return h("div", { class: "empty" }, dns.ruleQuery ? t("dns.rules.noMatches") : t("dns.rules.none"));

  return h("div", {}, dns.rules.map((rule) => h("div", { class: "crow dns-rule-row" },
    chip(rule.action === "allow" ? "ok" : rule.action === "redirect" ? "accent" : "danger", rule.action === "redirect" ? `→ ${rule.target_ip || ""}` : t("dns.act." + rule.action)),
    h("div", { class: "c-name ltr dns-rule-domain" }, rule.domain),
    h("div", { class: "c-name dns-rule-scope" }, scopeLabel(rule, ctx.dashboard), h("span", { class: "mono" }, rule.source || "")),
    h("button", { type: "button", class: "btn danger sm", "aria-label": t("dns.rule.delete"), onclick: () => deleteManualRule(ctx, rule) }, icon("x", 15)))));
}

function changeRulePage(ctx, offset) {
  dns.ruleOffset = Math.max(0, offset);
  loadRules(ctx);
}

function renderRulePager(ctx) {
  if (!dns.ruleTotal) return null;
  const current = Math.floor(dns.ruleOffset / RULE_PAGE_SIZE) + 1;
  const total = Math.ceil(dns.ruleTotal / RULE_PAGE_SIZE);
  return h("nav", { class: "fa dns-rule-pager", "aria-label": t("dns.rules.pagination"), "aria-live": "polite" },
    h("button", { type: "button", class: "btn ghost sm", disabled: dns.rulesLoading || dns.ruleOffset === 0, onclick: () => changeRulePage(ctx, dns.ruleOffset - RULE_PAGE_SIZE) }, t("dns.rules.previous")),
    h("span", { class: "sub" }, t("dns.rules.page").replace("{current}", num(current, 0)).replace("{total}", num(total, 0))),
    h("button", { type: "button", class: "btn ghost sm", disabled: dns.rulesLoading || dns.ruleOffset + RULE_PAGE_SIZE >= dns.ruleTotal, onclick: () => changeRulePage(ctx, dns.ruleOffset + RULE_PAGE_SIZE) }, t("dns.rules.next")));
}

function updateRuleView(ctx) {
  const results = document.querySelector(".dns-rule-results");
  const pager = document.querySelector(".dns-rule-pager-container");
  if (!results || !pager) {
    ctx.touch();
    return;
  }
  results.replaceChildren(renderRuleResults(ctx));
  const nav = renderRulePager(ctx);
  pager.replaceChildren(...(nav ? [nav] : []));
}

async function deleteManualRule(ctx, rule) {
  const key = "r" + rule.id;
  if (dns.busy.has(key)) return;
  dns.busy.add(key); ctx.touch();
  try {
    await api.del(`/api/dns/rules/${rule.id}`);
    toast(t("dr.done"));
  } catch (error) {
    toast(`${t("dr.failed")}: ${error.message}`, "bad");
  } finally {
    dns.busy.delete(key);
    await load(ctx, true);
  }
}

async function loadRules(ctx) {
  const request = ++dns.ruleRequest;
  dns.rulesLoading = true; dns.rulesError = false;
  updateRuleView(ctx);
  try {
    const page = await api.get(`/api/dns/rules/page?limit=${RULE_PAGE_SIZE}&offset=${dns.ruleOffset}&q=${encodeURIComponent(dns.ruleQuery)}`);
    if (request !== dns.ruleRequest) return;
    if (dns.ruleOffset > 0 && dns.ruleOffset >= page.total) {
      dns.ruleOffset = Math.max(0, Math.floor((page.total - 1) / RULE_PAGE_SIZE) * RULE_PAGE_SIZE);
      return await loadRules(ctx);
    }
    dns.rules = page.items || []; dns.ruleTotal = page.total || 0;
  } catch {
    if (request !== dns.ruleRequest) return;
    dns.rules = []; dns.ruleTotal = 0; dns.rulesError = true;
  } finally {
    if (request === dns.ruleRequest) { dns.rulesLoading = false; updateRuleView(ctx); }
  }
}

async function load(ctx, force) {
  if (dns.loading || (!force && dns.presets && dns.blocklists && Date.now() - dns.at < 30000)) return;
  dns.loading = true;
  try {
    const [presets, blocklists] = await Promise.all([
      api.get("/api/dns/presets"), api.get("/api/dns/blocklists"),
    ]);
    dns.presets = presets; dns.blocklists = blocklists; dns.at = Date.now();
    const feedIds = new Set(blocklists.map((feed) => Number(feed.id)));
    dns.selectedFeeds = new Set(
      [...dns.selectedFeeds].filter((id) => feedIds.has(Number(id))));
    await loadRules(ctx);
  } catch {
    dns.failAt = Date.now(); dns.presets = dns.presets || [];
    dns.blocklists = dns.blocklists || [];
  } finally { dns.loading = false; ctx.touch(); }
}
const field = (label, control, hint) => h("div", { class: "field" }, h("label", {}, label), control, hint ? h("span", { class: "sub" }, hint) : null);
const labelledField = (id, label, control, hint) => h("div", { class: "field" }, h("label", { for: id }, label), control, hint ? h("span", { class: "sub" }, hint) : null);
const feedErrorText = (code) => {
  const key = `dns.feed.error.${code}`;
  return t(key) === key ? t("dns.feed.error.generic") : t(key);
};

function targets(d) {
  const users = (d?.users || []).filter((u) => !u.protected);
  return { user: users.map((u) => [u.id, u.name]), device: users.flatMap((u) => (u.devices || []).map((x) => [x.id, `${x.name || x.mac} · ${u.name}`])) };
}
function scopeLabel(r, d) {
  if (r.scope === "global") return t("dns.scope.global");
  const tg = targets(d), list = r.scope === "user" ? tg.user : tg.device, hit = list.find(([id]) => id === r.scope_id);
  return `${t("dns.scope." + r.scope)}: ${hit ? hit[1] : "#" + r.scope_id}`;
}
function scopeTarget(scopeSel, d) {
  const target = h("select", { class: "select" });
  const fill = () => { const tg = targets(d); target.replaceChildren(...(tg[scopeSel.value] || []).map(([id, n]) => h("option", { value: String(id) }, n))); target.hidden = scopeSel.value === "global"; };
  scopeSel.addEventListener("change", fill); fill(); return target;
}
const scopeSelect = (cur = "global") => h("select", { class: "select" }, ["global", "user", "device"].map((s) => h("option", { value: s, selected: cur === s }, t("dns.scope." + s))));

export default function dnsTab(ctx) {
  const d = ctx.dashboard;
  if (Date.now() - dns.failAt > 10000) load(ctx);
  if (!dns.presets) return h("div", { class: "skeleton", style: { height: "280px" } });
  const act = async (key, fn, ok) => {
    if (dns.busy.has(key)) return; dns.busy.add(key); ctx.touch();
    try { await fn(); if (ok) toast(ok); } catch (e) { toast(`${t("dr.failed")}: ${e.message}`, "bad"); } finally { dns.busy.delete(key); await load(ctx, true); }
  };
  const feedErrorMessage = (error) => {
    let code = "fetch_failed";
    try { code = JSON.parse(error.message).code || code; } catch { /* use safe fallback */ }
    return feedErrorText(code);
  };
  const feedAction = async (key, fn, successKey) => {
    if (busy(key)) return;
    dns.busy.add(key); ctx.touch();
    try {
      await fn();
      dns.feedResult = t(successKey); dns.feedResultError = false;
    } catch (error) {
      dns.feedResult = feedErrorMessage(error); dns.feedResultError = true;
    } finally {
      dns.busy.delete(key); await load(ctx, true);
    }
  };
  const addFeed = (urlInput) => {
    if (!urlInput.reportValidity()) return;
    feedAction("feed:add", async () => {
      await api.post("/api/dns/blocklists", {
        name: dns.feedName.trim(), url: dns.feedUrl.trim(),
      });
      dns.feedName = ""; dns.feedUrl = "";
    }, "dns.feed.added");
  };
  const by = (id) => dns.presets.find((p) => p.id === id);
  const ultra = by("adblock-ultra"), std = by("ads-tracking");
  const on = !!(ultra?.enabled || std?.enabled);
  if (ultra?.enabled && !std?.enabled) dns.tier = "adblock-ultra"; else if (std?.enabled && !ultra?.enabled) dns.tier = "ads-tracking";
  const enable = (id) => api.post(`/api/dns/presets/${id}/enable`, { scope: "global" });
  const disable = (id) => api.post(`/api/dns/presets/${id}/disable`, { scope: "global" }).catch(() => {});
  const master = () => act("master", async () => {
    if (on) { await disable("adblock-ultra"); await disable("ads-tracking"); }
    else { const other = dns.tier === "adblock-ultra" ? "ads-tracking" : "adblock-ultra"; if (by(other)?.enabled) await disable(other); await enable(dns.tier); }
  });
  const pickTier = (id) => { dns.tier = id; ctx.touch(); if (on && !by(id)?.enabled) act("master", async () => { await disable(id === "adblock-ultra" ? "ads-tracking" : "adblock-ultra"); await enable(id); }); };
  const update = () => act("update", () => enable(dns.tier), t("dns.updated"));
  const busy = (k) => dns.busy.has(k);
  const selectFeeds = (ids) => {
    dns.selectedFeeds.clear();
    ids.forEach((id) => dns.selectedFeeds.add(Number(id)));
    syncFeedSelectionView();
  };
  const selectOneFeed = (feed, selected) => {
    if (selected) dns.selectedFeeds.add(Number(feed.id));
    else dns.selectedFeeds.delete(Number(feed.id));
    syncFeedSelectionView();
  };
  const setFeedEnabled = (feed, enabled) => feedAction(
    `feed:${feed.id}`,
    () => api.patch(`/api/dns/blocklists/${feed.id}`, { enabled }),
    enabled ? "dns.feed.enabled" : "dns.feed.disabled");
  const refreshFeed = (feed) => feedAction(
    `feed:${feed.id}`,
    () => api.post(`/api/dns/blocklists/${feed.id}/refresh`, {}),
    "dns.feed.refreshed");
  const removeFeed = (feed, event) => {
    if (dns.armedFeed !== feed.id) {
      dns.armedFeed = feed.id; ctx.touch();
      setTimeout(() => { if (dns.armedFeed === feed.id) { dns.armedFeed = null; ctx.touch(); } }, 4000);
      return;
    }
    dns.armedFeed = null;
    feedAction(`feed:${feed.id}`, () => api.del(`/api/dns/blocklists/${feed.id}`), "dns.feed.removed");
  };
  const searchInput = h("input", {
    class: "input ltr", type: "search", value: dns.ruleQuery,
    "aria-label": t("dns.rules.search"), autocomplete: "off",
    placeholder: t("dns.rules.search"),
    oninput: (event) => {
      dns.ruleQuery = event.target.value;
      clearTimeout(dns.searchTimer);
      dns.searchTimer = setTimeout(() => {
        dns.ruleOffset = 0; loadRules(ctx);
      }, 250);
    },
  });
  function ruleDialog(action = "block") {
    const scope = scopeSelect(), target = scopeTarget(scope, d);
    const act2 = h("select", { class: "select" }, ["block", "allow", "redirect"].map((a) => h("option", { value: a, selected: a === action }, t("dns.act." + a))));
    const domain = h("input", { class: "input ltr", required: true, placeholder: "ads.example.com", spellcheck: "false" });
    const ip = h("input", { class: "input ltr", placeholder: "192.168.2.1", spellcheck: "false" }), ipF = field(t("dns.redirect.ip"), ip); ipF.hidden = action !== "redirect";
    act2.onchange = () => { ipF.hidden = act2.value !== "redirect"; };
    openDialog({ title: t("dns.rule.add"), sub: t("dns.rule.sub"), submitLabel: t("add.submit"),
      content: [field(t("dns.domain"), domain), h("div", { class: "form-row" }, field(t("dns.act"), act2), field(t("dns.scope"), scope)), field(t("dns.target"), target), ipF, h("span", { class: "sub" }, t("dns.domain.h"))],
      onSubmit: async () => {
        const body = { scope: scope.value, scope_id: scope.value === "global" ? null : +target.value || null, action: act2.value, domain: domain.value.trim(), enabled: true };
        if (act2.value === "redirect") body.target_ip = ip.value.trim();
        await api.post("/api/dns/rules", body); toast(t("add.done")); await load(ctx, true); return true;
      } });
  }
  const cats = dns.presets.filter((p) => !TIERS.includes(p.id));
  const tier = (id, name, desc, tag) => h("button", { type: "button", class: "opt" + (dns.tier === id ? " on" : ""), "aria-pressed": String(dns.tier === id), onclick: () => pickTier(id) }, h("span", {}, h("b", {}, name, " ", chip(id === "adblock-ultra" ? "accent" : "", tag)), h("span", {}, desc)));
  const feedNameInput = h("input", {
    id: "dns-feed-name", class: "input", type: "text", maxlength: "80",
    value: dns.feedName, autocomplete: "off", "aria-label": t("dns.feed.name"),
    oninput: (event) => { dns.feedName = event.target.value; },
  });
  const feedUrlInput = h("input", {
    id: "dns-feed-url", class: "input ltr", type: "url", required: true,
    pattern: "https:" + "//.+", maxlength: "2048", value: dns.feedUrl,
    autocomplete: "url", spellcheck: "false", "aria-describedby": "dns-feed-help",
    "aria-label": t("dns.feed.url"),
    oninput: (event) => { dns.feedUrl = event.target.value; },
  });
  const dateText = (value) => value
    ? new Date(value * 1000).toLocaleString(getLang() === "ar" ? "ar-EG" : "en-US")
    : t("dns.feed.never");
  const feedRows = dns.blocklists.map((feed) => {
    const key = `feed:${feed.id}`, feedBusy = busy(key), bulkBusy = busy("feed:bulk");
    return h("div", { class: "dns-feed" },
      h("input", {
        class: "dns-feed-select", type: "checkbox", value: String(feed.id),
        checked: dns.selectedFeeds.has(Number(feed.id)), disabled: bulkBusy,
        "aria-label": t("dns.feed.selectOne").replace("{name}", feed.name || feed.url),
        onchange: (event) => selectOneFeed(feed, event.currentTarget.checked),
      }),
      h("div", { class: "dns-feed-details" },
        h("div", { class: "dns-feed-heading" },
          h("b", {}, feed.name || feed.url), chip(feed.enabled ? "ok" : "", feed.enabled ? t("dns.feed.enabled") : t("dns.feed.disabled"))),
        h("div", { class: "dns-feed-url ltr" }, iso(feed.url)),
        h("div", { class: "sub" }, `${t("dns.feed.lastRefresh")}: ${dateText(feed.last_success_at)}`),
        feed.last_error ? h("div", { class: "dns-feed-error", role: "status" }, feedErrorText(feed.last_error)) : null),
      h("div", { class: "dns-feed-actions" },
        h("button", { type: "button", class: "btn ghost sm", disabled: feedBusy || bulkBusy, "aria-label": `${t("dns.feed.refresh")} ${feed.name || feed.url}`, onclick: () => refreshFeed(feed) }, feedBusy ? t("dns.feed.working") : [icon("bolt", 14), t("dns.feed.refresh")]),
        h("button", { type: "button", role: "switch", class: "sw" + (feed.enabled ? " on" : ""), disabled: feedBusy || bulkBusy, "aria-checked": String(!!feed.enabled), "aria-label": `${t("dns.feed.toggle")} ${feed.name || feed.url}`, onclick: () => setFeedEnabled(feed, !feed.enabled) }, h("i")),
        h("button", { type: "button", class: "btn danger sm" + (dns.armedFeed === feed.id ? " armed" : ""), disabled: feedBusy || bulkBusy, "aria-label": `${t("dns.feed.remove")} ${feed.name || feed.url}`, onclick: (event) => removeFeed(feed, event) }, dns.armedFeed === feed.id ? t("dr.confirm") : t("dns.feed.remove"))));
  });
  const selectedFeedCount = dns.blocklists.filter(
    (feed) => dns.selectedFeeds.has(Number(feed.id))).length;
  const selectAllInput = h("input", {
    class: "dns-feed-select-all", type: "checkbox",
    checked: dns.blocklists.length > 0 && selectedFeedCount === dns.blocklists.length,
    disabled: busy("feed:bulk"),
    "aria-label": t("dns.feed.selectAll"),
    onchange: (event) => selectFeeds(event.currentTarget.checked
      ? dns.blocklists.map((feed) => feed.id) : []),
  });
  selectAllInput.indeterminate = selectedFeedCount > 0
    && selectedFeedCount < dns.blocklists.length;
  const feedToolbar = h("div", { class: "dns-feed-toolbar" },
    h("label", { class: "dns-feed-select-all-label" }, selectAllInput,
      h("span", {}, t("dns.feed.selectAll"))),
    h("div", { class: "dns-feed-bulk-actions" },
      h("button", {
        type: "button", class: "btn ghost sm dns-feed-remove-selected",
        disabled: busy("feed:bulk") || selectedFeedCount === 0,
        onclick: () => confirmBulkFeedRemoval(ctx, [...dns.selectedFeeds], false),
      }, t("dns.feed.removeSelected").replace("{count}", num(selectedFeedCount, 0))),
      h("button", {
        type: "button", class: "btn danger sm dns-feed-remove-all",
        disabled: busy("feed:bulk") || dns.blocklists.length === 0,
        onclick: () => confirmBulkFeedRemoval(
          ctx, dns.blocklists.map((feed) => feed.id), true),
      }, t("dns.feed.removeAll").replace("{count}", num(dns.blocklists.length, 0)))));
  const im = dns.imp, impScope = scopeSelect(im.scope), impTarget = scopeTarget(impScope, d);
  impScope.addEventListener("change", () => { im.scope = impScope.value; });
  const doImport = () => act("import", async () => {
    if (!im.text.trim()) throw new Error(t("dns.import.empty"));
    const res = await api.post("/api/dns/import", { text: im.text, format: im.format, scope: impScope.value, scope_id: impScope.value === "global" ? null : +impTarget.value || null, action: im.action });
    im.text = ""; im.result = t("dns.import.done").replace("{c}", res.created).replace("{s}", res.skipped);
  });

  return h("div", { class: "grid" },
    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dns.shield")), h("span", { class: "sub" }, t("dns.shield.d"))),
        h("div", { style: { display: "flex", gap: "10px", "align-items": "center" } }, chip(on ? "ok" : "", on ? t("dns.on") : t("dns.off")), sw(on, t("dns.shield"), master))),
      h("div", { class: "kv", style: { margin: "4px 0 14px" } },
        h("div", {}, h("span", { class: "k" }, t("dns.mode")), h("span", { class: "v" }, on ? (ultra?.enabled ? t("dns.tier.ultra") : t("dns.tier.std")) : "—"))),
      h("div", { class: "opts" }, tier("adblock-ultra", t("dns.tier.ultra"), t("dns.tier.ultra.d"), t("dns.tag.pro")), tier("ads-tracking", t("dns.tier.std"), t("dns.tier.std.d"), t("dns.tag.light"))),
      h("div", { class: "fa", style: { "margin-top": "14px" } },
        h("button", { type: "button", class: "btn ghost sm", disabled: busy("update") || busy("master"), onclick: update }, busy("update") ? t("dns.updating") : [icon("bolt", 15), t("dns.update")]),
        h("button", { type: "button", class: "btn ghost sm", onclick: () => ruleDialog("allow") }, icon("check", 15), t("dns.whitelist"))),
      h("p", { class: "sub", style: { "margin-top": "10px" } }, t("dns.update.h"))),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dns.cats")), h("span", { class: "sub" }, t("dns.cats.d")))),
      cats.length ? h("div", {}, cats.map((p) => h("div", { class: "row-sw", style: { padding: "12px 0" } },
        h("div", {}, h("b", {}, tr("dns.cat." + p.id + ".n", p.name)), h("span", {}, tr("dns.cat." + p.id + ".d", p.description)), p.enabled ? h("span", {}, scopeLabel({ scope: p.scope, scope_id: p.scope_id }, d)) : null),
        sw(p.enabled, p.name, () => act("p" + p.id, () => (p.enabled ? api.post(`/api/dns/presets/${p.id}/disable`, { scope: "global" }) : enable(p.id)))))))
        : h("div", { class: "empty" }, t("dns.cats.none"))),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dns.feed.title")), h("span", { class: "sub" }, t("dns.feed.subtitle")))),
      h("p", { class: "sub dns-feed-help", id: "dns-feed-help" }, t("dns.feed.help")),
      h("div", { class: "form-row dns-feed-form" },
        labelledField("dns-feed-name", t("dns.feed.name"), feedNameInput),
        labelledField("dns-feed-url", t("dns.feed.url"), feedUrlInput)),
      h("div", { class: "fa dns-feed-submit" },
        h("button", { type: "button", class: "btn primary", disabled: busy("feed:add") || busy("feed:bulk"), onclick: () => addFeed(feedUrlInput) }, busy("feed:add") ? t("dns.feed.adding") : [icon("plus", 15), t("dns.feed.add")])),
      dns.feedResult ? h("p", { class: "dns-feed-result" + (dns.feedResultError ? " is-error" : ""), role: dns.feedResultError ? "alert" : "status", "aria-live": "polite" }, dns.feedResult) : null,
      dns.blocklists.length
        ? [feedToolbar, h("div", { class: "dns-feed-list", "aria-label": t("dns.feed.title") }, feedRows)]
        : h("div", { class: "empty" }, t("dns.feed.empty"))),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dns.rules")), h("span", { class: "sub" }, t("dns.rules.d"))), h("button", { type: "button", class: "btn primary sm", onclick: () => ruleDialog() }, icon("plus", 15), t("dns.rule.add"))),
      h("div", { class: "dns-rule-search" }, field(t("dns.rules.search"), searchInput)),
      h("div", { class: "dns-rule-results" }, renderRuleResults(ctx)),
      h("div", { class: "dns-rule-pager-container" }, renderRulePager(ctx))),

    h("section", { class: "card c12" },
      h("header", { class: "card-h" }, h("div", {}, h("h3", {}, t("dns.import")), h("span", { class: "sub" }, t("dns.import.d")))),
      h("textarea", { class: "input ltr", rows: "6", spellcheck: "false", placeholder: "0.0.0.0 ads.example.com\n||tracker.example.net^", style: { height: "auto", padding: "10px 12px", width: "100%" }, oninput: (e) => { im.text = e.target.value; } }, im.text),
      h("div", { class: "form-row", style: { "margin-top": "12px" } },
        field(t("dns.import.format"), h("select", { class: "select", onchange: (e) => { im.format = e.target.value; } }, [["auto", t("dns.fmt.auto")], ["hosts", "hosts"], ["adblock", "AdBlock Plus"]].map(([v, l]) => h("option", { value: v, selected: im.format === v }, l)))),
        field(t("dns.act"), h("select", { class: "select", onchange: (e) => { im.action = e.target.value; } }, ["block", "allow"].map((a) => h("option", { value: a, selected: im.action === a }, t("dns.act." + a)))))),
      h("div", { class: "form-row", style: { "margin-top": "12px" } }, field(t("dns.scope"), impScope), field(t("dns.target"), impTarget)),
      h("div", { class: "fa", style: { "margin-top": "14px" } }, h("button", { type: "button", class: "btn primary", disabled: busy("import"), onclick: doImport }, busy("import") ? t("dns.importing") : t("dns.import.go"))),
      im.result ? h("p", { class: "sub", style: { "margin-top": "10px" } }, im.result) : null));
}
