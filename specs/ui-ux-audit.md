# QuotaManager Web UI — UX Audit

Scope: `web/js/` and `web/*.html` (dashboard). Findings grouped by requested category. Severity: **High** (data loss / destructive without guard / broken state), **Medium** (missing feedback/state), **Low** (polish/consistency).

## 1. Form validation feedback (error styling on invalid inputs)

- **Medium** `css/components.css:197-200` — `.input`/`.select` have no `:invalid`, `.invalid`, or `aria-invalid` styling; invalid fields look identical to valid ones.
- **Medium** `js/components/dialog.js:10-15` — dialog shows a single `.err` banner, but never marks the offending inputs; users must guess which field failed.
- **Low** `js/components/peopleDialogs.js:44` — MAC validation happens only after submit (`throw new Error(t("n.mac.bad"))`); no inline format feedback while typing.
- **Low** `js/pages/network.js:64-71` — MAC list add validates after Enter/button; no inline invalid state on the input.
- **Low** `js/pages/security.js:48-50` — firewall rule dialog accepts free-form IP/CIDR/port with no validation feedback.
- **Low** `js/pages/securityDns.js:67-70` — DNS rule dialog validates domain only on submit.

## 2. Required field indicators

- **Low** `js/components/peopleDialogs.js:19,33` — `required: true` is set, but labels have no visual required marker (asterisk) and no `aria-required`.
- **Low** `js/pages/quota.js:47-49`, `js/pages/security.js:58-60`, `js/pages/network.js:49,51`, `js/pages/securityDns.js:62`, `js/pages/vpn.js:31,39-41`, `js/pages/systemTabs.js:253-255` — required inputs rely on browser tooltip only; no consistent required indicator in labels.

## 3. Loading spinners / skeletons on async operations

- **Medium** `js/core/overviewData.js:6-23` — `loadDaily` failure leaves `ov.daily` null; overview shows skeleton with no error/retry state.
- **Medium** `js/pages/share.js:88` — when `sh.data` is null and `sh.err` is set, only the error text is shown; no retry button (relies on 10s poll).
- **Low** `js/pages/vpn.js:158-160` — VPN page skeleton while `!vpn.loaded`; if `loadAll` partially fails it still flips `loaded=true`, so empty lists can render without an error state.
- **Low** `js/pages/insights.js:258` — history load failure renders empty/error div but no explicit retry (manual refresh button exists).
- **Low** `js/pages/internet.js:13-20` — IPv6/Tailscale load failure only sets `failAt`; no user-facing retry beyond implicit retry after 10s.

## 4. Empty state messages

- **Low** `js/pages/overview.js:100,116,130` — chart tabs show `common.loading`/`ov.chart.idle` but no explicit empty-state illustration for zero data; minor.
- **Low** `js/pages/people.js:65` — users with no devices show `pp.nodevices` (good); filtering to zero users shows `pp.empty` (good).
- **Low** `js/pages/vpn.js:134` — empty routing list reuses `ov.nousers` text instead of a VPN-specific empty message.

## 5. Success / error feedback after form submissions

- **Medium** `js/pages/share.js:105-106` — deleting a shared file/text gives no toast (silent success/failure only via generic `dr.failed`).
- **Medium** `js/pages/share.js:107` — adding a text share gives no success toast.
- **Medium** `js/pages/people.js:27` — `act()` refreshes on success but never toasts success (errors are toasted).
- **Low** `js/components/userDrawer.js:71` — `mkRun` toasts success, but direct `run()` wrappers in drawers do not disable/re-enable inputs; feedback is toast-only.
- **Low** `js/pages/security.js:99-101` — revert/unban/ban use toasts, but unban success is not toasted.

## 6. Disabled states during async operations

- **High** `js/pages/network.js:26-28` — `run()` sets `busy=true` but does not call `ctx.touch()` until completion, so Save/Add/Delete buttons are not disabled while the request is in flight.
- **High** `js/pages/internet.js:85-87` — same `busy` pattern; WAN renew/auto-save buttons not disabled during requests.
- **Medium** `js/pages/quota.js:17-20` — `run()` sets `busy=true` but no immediate `ctx.touch()`; bundle save/top-up/reset-month buttons remain clickable.
- **Medium** `js/components/userDrawer.js:23-28,43,49` — save/top-up buttons are not disabled while `run()` is awaiting.
- **Medium** `js/pages/overview.js:47-51` — armed top-up button is not disabled during the `/api/bundle` request.
- **Medium** `js/pages/vpn.js:119-128` — routing toggles mutate UI optimistically but do not disable rows during the multi-request `set()`/`all()`.
- **Low** `js/components/dialog.js:13` — only the submit button is disabled; input fields remain editable during submission.

## 7. Confirmation dialogs for destructive actions

- **High** `js/pages/share.js:105` — single file delete (`del`) has no confirmation.
- **High** `js/pages/share.js:106` — single text delete (`delText`) has no confirmation.
- **High** `js/pages/share.js:113` — batch delete (`delete-batch`) has no confirmation.
- **Medium** `js/pages/security.js:82` — deleting a firewall rule splices config immediately with no confirmation.
- **Medium** `js/pages/securityDns.js:73` — deleting a DNS rule fires immediately with no confirmation.
- **Medium** `js/pages/vpn.js:125-128` — `route all` / `direct all` bulk changes have no confirmation.
- **Low** `js/pages/security.js:101` — unban has no confirmation (ban also none, but lower risk).

## 8. Input masking / formatting

- **Medium** `js/components/peopleDialogs.js:33` — MAC input has placeholder only; no auto-formatting of `:` separators or case normalization while typing.
- **Medium** `js/pages/network.js:49,68` — lease/MAC-list inputs require manual `:` formatting; validation only after submit.
- **Low** `js/pages/security.js:42-44,58-60` — IP/CIDR/port inputs have no masking or inline format hints beyond placeholders.
- **Low** `js/pages/quota.js:22-23` — number inputs accept arbitrary decimals; no formatting/clamping feedback.
- **Low** `js/components/dailyPlan.js:54-55` — heavy-day date input relies on native picker; no formatted display.

## 9. Autocomplete / autofill attributes

- **Low** `js/components/peopleDialogs.js:19,33-36` — name/MAC/owner inputs lack `autocomplete`/`spellcheck` where appropriate.
- **Low** `js/pages/network.js:49,51-52,68` — lease and MAC-list inputs lack `autocomplete`.
- **Low** `js/pages/security.js:42-44,58-60` — firewall inputs lack `autocomplete="off"`.
- **Low** `js/pages/securityDns.js:62` — domain input lacks `autocomplete="off"`.
- **Low** `js/pages/vpn.js:30-46` — node editor inputs lack `autocomplete` hints.
- **Low** `js/pages/quota.js:30` — top-up input lacks `inputmode`/autocomplete.

## 10. Error recovery (retry buttons)

- **Medium** `js/core/overviewData.js:6-8` — `loadDaily` has no retry/backoff; failure silently leaves no data.
- **Medium** `js/pages/share.js:13-18` — load failure shown as text only; recovery depends on 10s poll.
- **Low** `js/pages/internet.js:13-20` — load failure only retries after 10s; no manual retry button.
- **Low** `js/pages/network.js:13-18` — load failure retries after 10s; no manual retry.
- **Low** `js/pages/security.js:13-22` — load failure retries after 10s; no manual retry.

## 11. Optimistic UI updates

- **Low** `js/pages/vpn.js:120-123` — routing toggles update state before API but do not revert on failure (no rollback).
- **Low** `js/pages/network.js:26-41` — shaping/guest toggles await server refresh rather than optimistic update; minor latency.
- **Low** most page `run()` wrappers (`js/pages/quota.js:17-20`, `js/pages/security.js:88-98`) await full refresh; optimistic updates not used broadly.

## 12. Progress indicators for long operations

- **Low** `js/pages/updatesTab` (systemTabs.js:302-329) — update install shows busy text only; no progress percentage.
- **Low** `js/pages/vpn.js:64-69` — connect/disconnect shows busy text only; no progress for long connect.
- **Low** `js/pages/internet.js:90-93` — WAN renew shows confirm/busy only; no progress.
- **Medium** `js/core/api.js:13-25` — `fetch` has no timeout/abort; long or hung requests give no progress or recovery.

## Summary counts

- High: 4
- Medium: ~24
- Low: ~30+

Primary hotspots: `js/pages/network.js`, `js/pages/internet.js`, `js/pages/quota.js`, `js/pages/share.js`, `js/components/userDrawer.js`.
