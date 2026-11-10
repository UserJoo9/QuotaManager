import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import * as Tooltip from "@radix-ui/react-tooltip";
import { Sidebar } from "./Sidebar";
import { getPrefs, setPref, usePrefs } from "../lib/prefs";
import { loadLang, setLang } from "../lib/i18n";
import type { Dashboard } from "../lib/types";

beforeAll(async () => { await loadLang("en"); await loadLang("ar"); });
beforeEach(() => { setLang("en"); setPref("rail", false); setPref("navOrder", null); });
afterEach(() => { cleanup(); setLang("en"); vi.restoreAllMocks(); });

function Navigation({ mobile = false, onNavigate = () => {}, dash }: { mobile?: boolean; onNavigate?: () => void; dash?: Dashboard }) {
  const prefs = usePrefs();
  return <Tooltip.Provider><Sidebar route="network" live="live" rail={!mobile && prefs.rail} mobile={mobile} dash={dash}
    onNavigate={onNavigate} onLogout={() => {}} /></Tooltip.Provider>;
}

describe("sidebar navigation", () => {
  it("puts System and Help in the regular navigation as page links", () => {
    render(<Navigation />);
    const nav = within(screen.getByRole("navigation"));
    expect(nav.getByRole("link", { name: "System" }).getAttribute("href")).toBe("#/system");
    expect(nav.getByRole("link", { name: "Help" }).getAttribute("href")).toBe("#/help");
  });

  it("pins the quota summary outside scrolling navigation in both sidebar modes", () => {
    const dash: Dashboard = {
      bundle: { base_total_gb: 140, total_gb: 140, used_gb: 35, remaining_gb: 105, reset_day: 1,
        period_type: "renew_day", period_start: null, period_end: null, days_left: 22, recharges: [] },
      daily_enabled: false, free_mode: false, free_mode_enabled: true, users: [], rogue: [], internet: true, blocked_count: 0,
    };
    render(<Navigation dash={dash} />);
    const meter = screen.getByRole("meter", { name: "Monthly bundle" });
    expect(meter.closest(".qm-sidebar-scroll")).toBeNull();
    expect(screen.getByText(/105 GB left/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(screen.getByRole("meter", { name: "Monthly bundle" }).closest(".qm-sidebar-scroll")).toBeNull();
    expect(screen.getByRole("link", { name: "Monthly bundle: 25%" }).getAttribute("href")).toBe("#/quota");
  });
  it("keeps reordering out of ordinary navigation until requested", async () => {
    render(<Navigation />);
    expect(screen.getByRole("link", { name: "Network" }).getAttribute("aria-current")).toBe("page");
    expect(screen.queryByRole("button", { name: /^Reorder:/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reorder" }));
    await screen.findByRole("button", { name: "Done" });
    expect(screen.getAllByRole("button", { name: /^Reorder:/ }).length).toBe(10);
    fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));
    expect(getPrefs().navOrder).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /^Reorder:/ })).toBeNull());
  });

  it("keeps every destination named when collapsed, including Arabic", () => {
    render(<Navigation />);
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(getPrefs().rail).toBe(true);
    expect(screen.getAllByRole("link")).toHaveLength(11);
    for (const link of screen.getAllByRole("link")) expect(link.getAttribute("aria-label")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Expand sidebar" }).getAttribute("aria-expanded")).toBe("false");
    cleanup();
    setLang("ar");
    render(<Navigation />);
    for (const link of screen.getAllByRole("link")) expect(link.getAttribute("aria-label")).toBeTruthy();
  });

  it("offers an explicit close button on mobile", () => {
    const close = vi.fn();
    render(<Navigation mobile onNavigate={close} />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(close).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Collapse sidebar" })).toBeNull();
  });
});
