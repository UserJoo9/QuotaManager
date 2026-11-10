import { describe, expect, it } from "vitest";
import { parseNumber } from "../components/ui/forms";
import { mergeOrder, defaultOrder } from "../shell/routes";
import { toRenewal, fromRenewal } from "../components/domain/Renewal";
import { sanitize } from "./prefs";
import { isIPv4, isCidr4, normMac, MAC_RE } from "./format";
import { niceMax, downsample } from "../components/ui/charts";

describe("number fields never guess", () => {
  it("keeps empty as an error (not 0) unless allowed", () => {
    expect(parseNumber("", {}).error).toBeTruthy();
    expect(parseNumber("", { allowEmpty: true })).toEqual({ value: null, error: null });
  });
  it("rejects exponent, hex and junk", () => {
    for (const s of ["1e3", "0x10", "12abc", "--1", "1.2.3"]) expect(parseNumber(s, {}).value).toBeNull();
  });
  it("accepts Arabic-Indic digits and a comma decimal", () => {
    expect(parseNumber("١٢٫٥", {}).value).toBe(12.5);
    expect(parseNumber("12,5", {}).value).toBe(12.5);
  });
  it("enforces integer, decimals and bounds", () => {
    expect(parseNumber("3.5", { integer: true }).error).toBeTruthy();
    expect(parseNumber("1.234", { decimals: 2 }).error).toBeTruthy();
    expect(parseNumber("0", { min: 1 }).error).toBeTruthy();
    expect(parseNumber("65536", { max: 65535 }).error).toBeTruthy();
    expect(parseNumber("140", { min: 1, max: 100000, decimals: 2 }).value).toBe(140);
  });
});

describe("sidebar order", () => {
  it("defaults when nothing is saved", () => { expect(mergeOrder(null)).toEqual(defaultOrder()); });
  it("keeps a saved order and drops unknown ids", () => {
    const m = mergeOrder({ v: 1, groups: { main: ["quota", "ghost", "overview", "people", "insights"] } });
    expect(m.main).toEqual(["quota", "overview", "people", "insights"]);
  });
  it("slots a route added later into its default place", () => {
    const m = mergeOrder({ v: 1, groups: { main: ["people", "overview"] } });
    expect(m.main).toEqual(["people", "overview", "quota", "insights"]);
  });
});

describe("renewal mapping round-trips the backend's (period_type, reset_day)", () => {
  for (const [pt, d] of [["renew_day", 0], ["renew_day", 15], ["end_of_month", 0], ["end_of_month", 25]] as const) {
    it(`${pt}/${d}`, () => expect(fromRenewal(toRenewal(pt, d))).toEqual({ period_type: pt, reset_day: d }));
  }
});

describe("preferences migration", () => {
  it("drops retired colour themes but keeps language/theme/density", () => {
    const p = sanitize({ lang: "ar", theme: "light", density: "compact", accent: "violet", tone: "nord", particles: "rain", fx: "rich" });
    expect(p.lang).toBe("ar"); expect(p.theme).toBe("light"); expect(p.density).toBe("compact");
    expect(p as unknown as Record<string, unknown>).not.toHaveProperty("tone");
  });
  it("rejects a broken nav order", () => { expect(sanitize({ navOrder: { v: 9, groups: {} } }).navOrder).toBeNull(); });
});

describe("identifiers", () => {
  it("validates IPv4 / CIDR / MAC strictly", () => {
    expect(isIPv4("192.168.2.50")).toBe(true); expect(isIPv4("192.168.2.256")).toBe(false); expect(isIPv4("01.2.3.4")).toBe(false);
    expect(isCidr4("10.0.0.0/8")).toBe(true); expect(isCidr4("10.0.0.0/33")).toBe(false);
    expect(MAC_RE.test(normMac("AA-BB-CC-DD-EE-FF"))).toBe(true);
  });
});

describe("charts", () => {
  it("rounds axis maxima to readable values", () => { expect(niceMax(7.3)).toBe(10); expect(niceMax(180)).toBe(200); expect(niceMax(0)).toBe(1); });
  it("downsamples long series but keeps short ones", () => {
    const pts = Array.from({ length: 1000 }, (_, i) => ({ v: i }));
    expect(downsample(pts, 100)).toHaveLength(100); expect(downsample(pts.slice(0, 10), 100)).toHaveLength(10);
  });
});

import { crc32 } from "./liveTransfer";
describe("device-to-device transfer", () => {
  it("CRC-32 matches the standard check value (ZIP entries depend on it)", () => {
    const c = crc32(0xFFFFFFFF, new TextEncoder().encode("123456789"));
    expect(((c ^ 0xFFFFFFFF) >>> 0).toString(16)).toBe("cbf43926");
    // chunked = one shot (files are streamed in 512 KB slices)
    const data = new Uint8Array(100_003).map((_, i) => (i * 31) & 255);
    let a = 0xFFFFFFFF; for (let i = 0; i < data.length; i += 4096) a = crc32(a, data.subarray(i, i + 4096));
    expect(a).toBe(crc32(0xFFFFFFFF, data));
  });
});
