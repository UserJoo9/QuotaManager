import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import * as Tooltip from "@radix-ui/react-tooltip";
import type { Dashboard } from "../lib/types";
import { loadLang, setLang, t } from "../lib/i18n";
import People from "./People";

const state = vi.hoisted(() => ({ dashboard: undefined as Dashboard | undefined, post: vi.fn() }));
vi.mock("../lib/session", () => ({ useDashboard: () => state.dashboard }));
vi.mock("../lib/queries", () => ({ useLiveTraffic: () => ({ data: undefined }) }));
vi.mock("../lib/api", () => ({ api: { post: state.post } }));
vi.mock("../lib/actions", () => ({ actions: {}, useAction: () => ({ pending: null, run: async (_key: string, fn: () => Promise<unknown>) => { await fn(); return true; } }) }));
vi.mock("../shell/App", () => ({ PageActions: () => null }));
vi.mock("../components/domain/PersonSheet", () => ({ PersonSheet: () => null, DeviceSheet: () => null }));
vi.mock("../components/domain/AddDialogs", () => ({ AddPersonDialog: () => null, AddDeviceDialog: () => null }));
vi.mock("../components/ui/overlays", () => ({ useToast: () => vi.fn(), ConfirmDialog: () => null }));

beforeAll(async () => { await loadLang("en"); setLang("en"); });
beforeEach(() => {
  state.post.mockReset().mockResolvedValue({ ok: true });
  state.dashboard = {
    bundle: { base_total_gb: 100, total_gb: 100, used_gb: 0, remaining_gb: 100, reset_day: 1,
      period_type: "renew_day", period_start: null, period_end: null, days_left: 10, recharges: [] },
    daily_enabled: false, free_mode: false, free_mode_enabled: true, users: [],
    rogue: [{ mac: "aa:bb:cc:dd:ee:ff", ip: "192.168.1.50", blocked: true }],
    unmanaged_block: true, internet: true, blocked_count: 1,
  };
});
afterEach(cleanup);
const show = () => render(<Tooltip.Provider><People /></Tooltip.Provider>);

describe("upstream unmanaged-device controls", () => {
  it("shows the cut-off status and sends the device address when managing it", async () => {
    show();
    expect(screen.getByText(t("pp.rogue.cut"))).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("pp.rogue.manage") }));
    await waitFor(() => expect(state.post).toHaveBeenCalledWith("/api/rogue/adopt", { mac: "aa:bb:cc:dd:ee:ff", ip: "192.168.1.50" }));
  });

  it("can allow a fixed-IP device to keep internet access", async () => {
    show();
    fireEvent.click(screen.getByRole("button", { name: t("pp.rogue.allow") }));
    await waitFor(() => expect(state.post).toHaveBeenCalledWith("/api/rogue/allow", { mac: "aa:bb:cc:dd:ee:ff" }));
  });

  it("keeps the policy switch available when disabled with no unmanaged devices", async () => {
    if (!state.dashboard) throw new Error("Missing dashboard fixture");
    state.dashboard.rogue = [];
    state.dashboard.unmanaged_block = false;
    show();
    const control = screen.getByRole("switch", { name: t("pp.rogue.sw") });
    expect(control.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(control);
    await waitFor(() => expect(state.post).toHaveBeenCalledWith("/api/unmanaged-policy", { enabled: true }));
  });
});
