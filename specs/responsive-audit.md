# QuotaManager Web UI — Responsive/Mobile Audit

Scope: `web/css/*.css`, `web/assets/*.css`, `web/*.html`. Static audit (no runtime). Breakpoints present: 480, 600, 640, 900, 1100, 1180/1200-ish.

Severity: **H** high (breaks layout/usability on mobile), **M** medium, **L** low.

## 1. Breakpoints / mobile coverage

- **M** `css/layout.css:60` — only `.c5,.c7,.c6,.c4` collapse at 1180px; `.c5`/`.c7` asymmetric spans become full-width but no intermediate 2-col step; fine but no mobile-specific tightening below 900 beyond main padding.
- **M** `css/components.css:103` — `.crow` collapse only at 640px; between 640–900px the 4-col grid (`36px minmax(120,1fr) minmax(130,1.2fr) auto`) can overflow on ~700px widths because min widths sum ~286px + gaps; should collapse at 900px to match people table.
- **M** `css/components.css:138` — people `.urow`/`.drow` collapse at 900px, but `.lhead` hides; okay. However `.lhead,.urow` minmax(220px,1.3fr) etc. (line 111) can exceed viewport between 640–900px before collapse.
- **L** `assets/share.css:133` — only one mobile breakpoint at 480px; nothing for 640–900 tablet; `.live-peers-grid` minmax(150px) may be cramped but wraps.

## 2. Touch targets < 44x44 on mobile

- **H** `css/base.css:45` — `.btn.sm` height 32px (padding 0 12px) — below 44.
- **H** `css/base.css:55` — `.icon-btn` 40x40 — below 44.
- **H** `css/layout.css:8` — `.collapse-btn` 32x32.
- **H** `css/layout.css:32` — `.side-foot .icon-btn` 34x34.
- **M** `css/layout.css:15` — `.nav-i` height `var(--navh)` = 42px (36px compact) — below 44; `.nav-i` is a primary nav target.
- **M** `css/components.css:115` — `.chev` 36x36.
- **M** `css/components.css:63` (`.seg button` height 32px) and `css/base.css:63` — below 44.
- **M** `css/components.css:235` — `.tabs button` height 42px — just under 44.
- **L** `assets/share.css:67` — `.btn-batch` height 30px.
- **L** `assets/share.css:91` — `.btn-action` min-height 30px.
- **L** `assets/share.css:123` — `.peer-send-btn` height 30px.
- **L** `css/components.css:352` — `.dq-x` small tap area (padding 0 2px).
- **L** `css/base.css:57` — `.sw` 42x24 switch (height 24) — below 44 though width okay; vertical tap target small.

## 3. Horizontal overflow / scroll on mobile

- **H** `css/components.css:106` — `.search` `min-width:240px; flex:0 1 340px` inside `.top` (flex-wrap). On 320–360px viewports the search keeps 240px min width and can force horizontal scroll if siblings don't wrap in time; no mobile collapse to full width.
- **M** `css/components.css:129` — `.d-main` `width:calc(100% + 16px)` with negative margins can extend beyond card padding and cause horizontal scroll on mobile.
- **M** `css/components.css:204`/`css/components.css:277` — `.vstate`/`.hero .state` `min-width:220px` can overflow narrow cards before wrapping (flex-wrap present but min-width rigid).
- **M** `css/components.css:219` — `.node .grow` `min-width:160px` similar.
- **L** `css/layout.css:47` — `.main` padding `22px 32px 56px` at desktop; mobile override sets 18px 16px (layout.css:81) okay. But `.card` padding `var(--pad)` (22px default) not reduced on mobile (no mobile override) — contributes to crowding but not overflow.

## 4. Text truncation / overflow in narrow viewports

- **M** `css/components.css:92` — `.c-num` `white-space:nowrap` with `justify-content:space-between` and `gap:8px` inside a grid column that can shrink; may overflow rather than wrap.
- **M** `css/components.css:123` — `.u-num` `white-space:nowrap` in narrow people row.
- **M** `css/components.css:134` — `.d-live` `white-space:nowrap` in collapsed drow (grid-column:1/-1) on mobile; can overflow card width.
- **L** `css/components.css:65` — `.tk-top span` ellipsis okay but `.tk` min-width 0; fine.
- **L** `assets/share.css:120` — `.peer-name` ellipsis okay.

## 5. Grid columns that don't collapse properly

- **H** `css/components.css:18` — `.kpi-grid` `repeat(3, minmax(0,1fr))` with no responsive override; stays 3 columns on 320px (each ~96px), cramped.
- **M** `css/components.css:62` — `.talkers` `repeat(3, ...)` no mobile collapse.
- **M** `css/components.css:26` — `.net-split` `1fr 1fr` no mobile collapse (acceptable but tight at 320).
- **M** `css/components.css:194` — `.form-row` `1fr 1fr` no mobile collapse; two inputs side-by-side on phone.
- **M** `css/components.css:353` — `.dq-add` `1fr 90px auto` no mobile collapse; input shrinks on 320px.
- **M** `css/components.css:281` — `.g-4` collapses only at 640 to 2 columns (not 1); `.g-6` to 2 at 640; okay-ish.
- **L** `css/components.css:74` — `.tiles` `auto-fit minmax(200px,1fr)` wraps to 1 column below ~400px, but between 400–600px shows 2 narrow columns (each ~200px) — may be tight.

## 6. Fixed widths that break on small screens

- **M** `css/components.css:106` — `.search` `flex:0 1 340px` + `min-width:240px` (see overflow).
- **L** `css/components.css:3` — `.gauge` 190x190 fixed; okay but large on 320px viewport (59% width); could scale.
- **L** `css/components.css:149` — `.drawer` `width:min(430px,100%)` okay.
- **L** `css/components.css:188` — `.dialog` `width:min(520px,94vw)` okay.
- **L** `css/components.css:226`/`css/components.css:298` — `.scroll` max-height 380px and `.live-body` height 390px fixed; on short mobile screens these fixed heights can exceed viewport and feel oversized (not break, but poor).
- **L** `css/components.css:229` — `.terminal` height 260px fixed.

## 7. Modal/dialog sizing on mobile

- **M** `css/components.css:188` — `.dialog` `top:8vh; max-height:84vh` with no mobile-specific padding reduction; `.dialog-body` padding 20px 22px (line 192) and header 18px 22px (line 190) stay large on 320px.
- **L** `css/components.css:189` — `.dialog.wide` `min(640px,94vw)` okay.
- **L** `assets/share.css:103` — `.modal-backdrop` padding 16px; `.modal-box` max-width 380px okay.

## 8. Sidebar behavior on mobile

- **M** `css/layout.css:70-83` — off-canvas transform uses `translateX(calc(-105%*var(--dir)*-1))` (line 72) plus explicit RTL/LTR overrides (lines 73-74); logic works but duplicated/fragile. Also `.shell.rail` on mobile reverts to full labels (lines 77-79) which is desirable but means rail preference is ignored on mobile — acceptable.
- **L** `css/layout.css:71` — `.shell{display:block}` on mobile removes flex; main content no longer beside sidebar; okay.
- **L** `css/layout.css:54` — `.menu-btn{display:none}` base, shown at 900px (line 80); verify JS toggles `.nav-open` on `.shell` — CSS present.

## 9. Table/grid layouts needing horizontal scroll

- **M** `css/components.css:111` — `.lhead,.urow` grid with rigid minmax widths (220, 210, 150, 56) sums ~636px + gaps; on 640–900px before collapse at 900, can overflow; no horizontal scroll wrapper.
- **M** `css/components.css:127` — `.drow` similar (220, 210, 150, 56) with `padding-inline-start:72px` adds width; can overflow at 640–900.
- **L** `report.html:33-46` — table gets `overflow-x:auto` only under 600px; between 600–900px the 4-column table relies on card width; okay-ish.

## 10. Font sizes too small on mobile (<12px)

- **M** `css/components.css:326` — `.ins-col span` 10px.
- **M** `css/components.css:59-60` — `.ylab`/`.xlab` 10.5px (chart axis labels).
- **L** `css/base.css:38` / `css/components.css:270` / `assets/share.css:21` etc. — 11.5px chips/badges (acceptable for dense UI but <12).
- **L** `css/base.css:66` — `.seg em` 11px.
- **L** `assets/share.css:99` — `.text-meta` 11.5px.

## 11. Spacing/padding too large on mobile

- **M** `css/layout.css:61` — `.card` padding `var(--pad)` = 22px default (16px only in compact density); no mobile reduction of `--pad` outside compact mode.
- **M** `css/components.css:190-192` — dialog padding 18-22px / 20-22px unchanged on mobile.
- **M** `css/components.css:151` / `css/components.css:156` — drawer header/body padding 20-22px unchanged on mobile.
- **L** `assets/share.css:133` — reduces body padding to 10px but only at 480px; tablet (480–900) keeps 14px body padding (acceptable).

## Accessibility notes (related)

- **M** `web/guest_pending.html:5` and `web/share.html:5` (viewport meta) — `maximum-scale=1.0, user-scalable=no` disables zoom; accessibility issue on mobile.
- **L** `css/base.css:183-189` — global `* { -webkit-touch-callout: none; }` can hinder text selection; mitigated for inputs.

## Summary

Most critical: touch targets under 44px (buttons/icons/switch), grids that don't collapse (kpi-grid, talkers, form-row, dq-add), `.search` min-width causing horizontal overflow, and crow/people table minmax widths causing overflow at tablet widths. Recommend adding a `@media (max-width:640px)` (and 900px for tables) block to collapse grids, reduce paddings, enlarge tap targets, and make `.search` full-width on mobile.<|end|><|start|>terminal to=assistant<|channel|>analysis<|message|>Now final summary.<|end|><|start|><|start|>assistant<|channel|>final<|message|>Audit complete. Wrote structured findings to `/home/abed/Projects/QuotaManager/RESPONSIVE_AUDIT.md`.

Key findings (high severity):
- Touch targets under 44px: `.btn.sm` (32px), `.icon-btn` (40px), `.collapse-btn` (32px), `.side-foot .icon-btn` (34px), `.nav-i` (42/36px), `.chev` (36px), `.seg button` (32px), `.tabs button` (42px) — base.css/components.css/layout.css.
- Grids not collapsing on mobile: `.kpi-grid` (3 cols), `.talkers` (3 cols), `.net-split`, `.form-row`, `.dq-add` — components.css.
- Horizontal overflow risk: `.search` min-width 240px (components.css:106), `.d-main` width calc(100%+16px), rigid people table minmax widths (components.css:111/127).
- People/consumer tables overflow at 640–900px before their 900px collapse.
- Small fonts: 10px (ins-col span), 10.5px (chart axes).
- Accessibility: guest_pending.html & share.html disable user zoom.

Lower severity: modal/drawer padding not reduced on mobile, fixed heights (terminal 260px, live-body 390px), density padding not auto-reduced outside compact mode. Full list with file:line and severity in the report. No code changes made. Done.<|end|><|start|>tool_call_begin|xml<|message|><rfl:parameter name="name">
todo_list