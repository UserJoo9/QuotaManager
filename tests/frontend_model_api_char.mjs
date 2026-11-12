import { mapDashboardResponse, mapLoginResponse, mapBundleRequest } from "../web/js/model/api/index.js";

function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL:", msg);
    process.exit(1);
  }
}

const raw = {
  users: [
    {
      id: 1,
      name: "Alice",
      devices: [{ id: 2, name: "dev", mac: "aa", ip: "10.0.0.2", connected: true }],
      daily: { active: true, mode: "spread", gb: 5, carry: "next_day", split: "open", self_edit: true, heavy: { a: 1 } },
    },
  ],
  bundle: {
    base_total_gb: 100,
    total_gb: 120,
    used_gb: 30,
    remaining_gb: 90,
    days_left: 5,
    period_type: "renew_day",
    period_start: "s",
    period_end: "e",
    reset_day: 10,
    recharges: [{ id: 1, target_type: "all", target_id: 0, gb: 5, expires_at: 0, recurring: true, comment: "c", created_at: "t" }],
  },
  vpn: null,
  vpn_share: null,
  gateway_blocked_desired: true,
  gateway_blocked_programmed: false,
  unmanaged_block: false,
  rogue: [{ x: 1 }],
  security: { ok: true },
  version: "1.2",
};

const d = mapDashboardResponse(raw);
assert(d.users.length === 1, "users length");
const u = d.users[0];
assert(u.name === "Alice", "user name");
assert(u.devices.length === 1, "devices length");
const dev = u.devices[0];
assert(dev.id === 2 && dev.name === "dev" && dev.mac === "aa" && dev.ip === "10.0.0.2" && dev.connected === true, "device fields");
assert(u.daily && u.daily.active === true && u.daily.mode === "spread" && u.daily.gb === 5 && u.daily.carry === "next_day" && u.daily.split === "open" && u.daily.self_edit === true, "daily fields");
assert(JSON.stringify(u.daily.heavy) === JSON.stringify({ a: 1 }), "daily heavy");
const b = d.bundle;
assert(b.base_total_gb === 100, "base_total_gb");
assert(b.total_gb === 120 && b.used_gb === 30 && b.remaining_gb === 90 && b.days_left === 5, "bundle numbers");
assert(b.period_type === "renew_day" && b.period_start === "s" && b.period_end === "e" && b.reset_day === 10, "bundle period");
assert(b.recharges.length === 1 && b.recharges[0].gb === 5 && b.recharges[0].recurring === true, "recharges");
assert(d.gateway_blocked_desired === true && d.gateway_blocked_programmed === false && d.unmanaged_block === false, "gateway flags");
assert(JSON.stringify(d.rogue) === JSON.stringify([{ x: 1 }]), "rogue");
assert(d.security && d.security.ok === true, "security");
assert(d.version === "1.2", "version");
assert(d.vpn === null && d.vpn_share === null, "vpn nulls");

// coalesce fallback for base_total_gb
const d2 = mapDashboardResponse({ bundle: { total_gb: 50 } });
assert(d2.bundle.base_total_gb === 50, "base_total_gb fallback");

// null daily
const d3 = mapDashboardResponse({ users: [{ id: 3, daily: null }], bundle: {} });
assert(d3.users[0].daily === null, "null daily");

// device defaults
const d4 = mapDashboardResponse({ users: [{ id: 4, devices: [{ id: 5 }] }], bundle: {} });
const dev4 = d4.users[0].devices[0];
assert(dev4.name === "" && dev4.mac === "" && dev4.ip === null && dev4.ip6 === null, "device defaults 1");
assert(dev4.connected === false && dev4.blocked === false && dev4.gateway === false, "device defaults 2");

// login response
const lr = mapLoginResponse({ authed: 1, totp: "x" });
assert(lr.authed === true && lr.totp === "x", "login response");
const lr2 = mapLoginResponse({});
assert(lr2.authed === false && lr2.totp === null, "login response empty");

// bundle request
const br = mapBundleRequest({ total_gb: 10, reset_day: 5, period_type: "p" });
assert(br.total_gb === 10 && br.reset_day === 5 && br.period_type === "p", "bundle request");

console.log("OK");
