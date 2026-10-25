"""Per-destination traffic (conntrack) — where a device's bytes actually go."""

from __future__ import annotations

from quota import flows
from quota.flows import (FlowSampler, parse_conntrack_line, parse_reply_line,
                         record_dns_reply)

TCP = ("ipv4     2 tcp      6 431999 ESTABLISHED src=192.168.2.186 dst={dst} "
       "sport={sp} dport=443 packets=10 bytes={up} src={dst} dst=192.168.1.110 "
       "sport=443 dport={sp} packets=12 bytes={down} [ASSURED] mark=0 use=1")


class _R:
    """Reverse resolver stub: never touches the network, never starts a thread."""
    def __init__(self, names=None):
        self.names = names or {}

    def lookup(self, ip):
        return self.names.get(ip)


def _sampler(tmp_path, lines, resolver=None):
    p = tmp_path / "ct"
    p.write_text("\n".join(lines) + "\n")
    t = [0.0]
    s = FlowSampler(proc_path=p, resolver=resolver or _R(), owner_lookup=False,
                    clock=lambda: t[0])
    return s, p, t


def test_parse_tcp_and_udp_lines_and_ignore_the_rest():
    f = parse_conntrack_line(TCP.format(dst="157.240.1.35", sp=51234, up=9000, down=170000))
    assert (f.proto, f.client, f.server, f.dport, f.up, f.down) == (
        "tcp", "192.168.2.186", "157.240.1.35", 443, 9000, 170000)
    u = parse_conntrack_line("ipv4 2 udp 17 29 src=192.168.2.5 dst=8.8.8.8 sport=1 dport=53 "
                             "packets=1 bytes=70 src=8.8.8.8 dst=192.168.1.110 sport=53 dport=1 "
                             "packets=1 bytes=120 mark=0")
    assert (u.up, u.down) == (70, 120)
    v6 = parse_conntrack_line("ipv6 10 tcp 6 100 ESTABLISHED src=fd00:da7a:2::101 dst=2a03:2880::1 "
                              "sport=5 dport=443 packets=1 bytes=500 src=2a03:2880::1 "
                              "dst=2001:db8::1 sport=443 dport=5 packets=1 bytes=900")
    assert v6.client == "fd00:da7a:2::101" and v6.down == 900
    assert parse_conntrack_line("ipv4 2 icmp 1 29 src=1.1.1.1 dst=2.2.2.2 type=8") is None
    # accounting off => no bytes= => unusable
    assert parse_conntrack_line("ipv4 2 tcp 6 10 ESTABLISHED src=1.1.1.1 dst=2.2.2.2 "
                                "sport=1 dport=2 src=2.2.2.2 dst=1.1.1.1 sport=2 dport=1") is None


def test_first_sample_is_only_a_baseline_then_deltas_count(tmp_path):
    s, p, t = _sampler(tmp_path, [TCP.format(dst="157.240.1.35", sp=1, up=1000000, down=5000000)])
    s.sample()
    assert s.latest == {}                       # old flow: no fake burst at startup
    t[0] = 15.0
    p.write_text(TCP.format(dst="157.240.1.35", sp=1, up=3000000, down=5000100) + "\n")
    s.sample()
    assert s.window_s == 15.0
    assert s.latest["192.168.2.186"]["157.240.1.35"] == [2000000, 100]   # an UPLOAD


def test_new_flow_after_baseline_counts_fully_and_closed_flows_vanish(tmp_path):
    s, p, t = _sampler(tmp_path, [TCP.format(dst="157.240.1.35", sp=1, up=10, down=10)])
    s.sample()
    t[0] = 15.0
    p.write_text(TCP.format(dst="31.13.1.1", sp=2, up=800000, down=1000) + "\n")
    s.sample()
    assert list(s.latest["192.168.2.186"]) == ["31.13.1.1"]


def test_lan_destinations_are_ignored(tmp_path):
    s, p, t = _sampler(tmp_path, [TCP.format(dst="192.168.1.50", sp=1, up=1, down=1)])
    s.sample()
    t[0] = 15.0
    p.write_text(TCP.format(dst="192.168.1.50", sp=1, up=900000, down=900000) + "\n")
    s.sample()
    assert s.latest == {}


def test_top_destinations_names_rates_and_shares(tmp_path):
    record_dns_reply("Oct 21 14:00:00 dnsmasq[1]: 7 192.168.2.186/5353 reply rupload.facebook.com is 157.240.1.35")
    s, p, t = _sampler(tmp_path, [
        TCP.format(dst="157.240.1.35", sp=1, up=0, down=0),
        TCP.format(dst="104.18.1.1", sp=2, up=0, down=0)],
        resolver=_R({"104.18.1.1": "a1.edge.tiktokcdn.com"}))
    s.sample()
    t[0] = 10.0
    p.write_text(TCP.format(dst="157.240.1.35", sp=1, up=7_500_000, down=1000) + "\n"
                 + TCP.format(dst="104.18.1.1", sp=2, up=2_500_000, down=0) + "\n"
                 + TCP.format(dst="8.8.4.4", sp=3, up=100, down=100) + "\n")   # noise < 4 KB
    s.sample()
    top = s.top_destinations({"192.168.2.186"})
    assert [d["domain"] for d in top] == ["facebook.com", "tiktokcdn.com"]
    assert top[0]["up_bps"] == int(7_500_000 * 8 / 10) and top[0]["resolved"]
    assert top[0]["share_pct"] == 75.0
    # an unresolvable IP is shown as the IP itself (still a usable "link")
    s.latest = {"192.168.2.186": {"8.8.4.4": [90000, 0]}}
    assert s.top_destinations({"192.168.2.186"})[0]["domain"] == "8.8.4.4"


def test_reply_line_parsing():
    assert parse_reply_line("12 1.2.3.4/55 reply www.x.com is 93.184.216.34") == ("www.x.com", "93.184.216.34")
    assert parse_reply_line("reply www.x.com is <CNAME>") is None
    assert parse_reply_line("reply www.x.com is NXDOMAIN") is None
    assert parse_reply_line("query[A] www.x.com from 1.2.3.4") is None


def test_accounting_toggle_is_best_effort(tmp_path):
    f = tmp_path / "acct"
    f.write_text("0\n")
    assert flows.enable_conntrack_accounting(f) is True and f.read_text().strip() == "1"
    assert flows.enable_conntrack_accounting(tmp_path / "missing" / "x") is False


def test_live_traffic_exposes_top_destinations(tmp_path):
    """The analytics payload carries the snapshot's per-device destinations (and
    drops them when the snapshot is stale)."""
    import asyncio
    import time
    from types import SimpleNamespace

    from quota import db as _db
    from quota.engine import EngineCounters, EngineSnapshot
    from quota.history_analytics import get_live_traffic_analytics

    dest = [{"domain": "facebook.com", "ip": "157.240.1.35", "resolved": True,
             "up_bps": 2_000_000, "down_bps": 1000, "share_pct": 100.0}]

    async def go():
        db = _db.Database(str(tmp_path / "f.db"))
        await db.connect()
        try:
            dev = SimpleNamespace(id=1, mac="aa:bb:cc:dd:ee:01", name="17", user_id=1)
            live = EngineSnapshot(by_ip={"192.168.2.5": EngineCounters(up=3_000_000, down=0)},
                                  flows_by_mac={dev.mac: dest})
            res = await get_live_traffic_analytics(db, [dev], {dev.mac: "192.168.2.5"}, live, {})
            assert res["devices"][0]["top_destinations"] == dest
            old = EngineSnapshot(flows_by_mac={dev.mac: dest}, ts=time.time() - 600)
            res = await get_live_traffic_analytics(db, [dev], {dev.mac: "192.168.2.5"}, old, {})
            assert res["devices"][0]["top_destinations"] == []
        finally:
            await db.close()

    asyncio.run(go())


# ---------------------------------------------------------------- IP owner (RDAP)

_RDAP = {
    "name": "NET-149-154-160-0-1",
    "startAddress": "149.154.160.0", "endAddress": "149.154.175.255",
    "cidr0_cidrs": [{"v4prefix": "149.154.160.0", "length": 20}],
    "entities": [{"roles": ["registrant"],
                  "vcardArray": ["vcard", [["fn", {}, "text", "Telegram Messenger Inc"]]]}],
}


def test_owner_from_rdap_name_and_range():
    owner, net = flows.owner_from_rdap(_RDAP, "149.154.166.111")
    assert owner == "Telegram Messenger Inc"
    assert str(net) == "149.154.160.0/20"
    assert flows.owner_from_rdap({}, "8.8.8.8") == (None, None)


def test_owner_resolver_looks_up_once_per_range_and_persists(tmp_path):
    import time
    calls = []

    def fetch(ip):
        calls.append(ip)
        return _RDAP

    cache = tmp_path / "owners.json"
    r = flows._OwnerResolver(fetch=fetch, cache_path=cache, min_interval=0)
    assert r.lookup("149.154.166.111") is None          # queued, answered later
    for _ in range(100):
        if r.lookup("149.154.166.111"):
            break
        time.sleep(0.02)
    assert r.lookup("149.154.166.111") == "Telegram Messenger Inc"
    assert r.lookup("149.154.170.5") == "Telegram Messenger Inc"   # same /20, no new query
    assert len(calls) == 1
    r._last_save = 0
    r._save()
    again = flows._OwnerResolver(fetch=lambda ip: None, cache_path=cache)
    assert again.lookup("149.154.161.9") == "Telegram Messenger Inc"


def test_unknown_ip_is_remembered_as_negative(tmp_path):
    import time
    calls = []
    r = flows._OwnerResolver(fetch=lambda ip: calls.append(ip) or None, min_interval=0)
    r.lookup("93.184.216.34")
    time.sleep(0.2)
    assert r.lookup("93.184.216.34") is None
    r.lookup("93.184.216.34")
    time.sleep(0.1)
    assert len(calls) == 1


def test_label_order_name_then_owner_then_ip(tmp_path):
    class Owners:
        def lookup(self, ip):
            return "Telegram Messenger Inc" if ip.startswith("149.") else None

    s = FlowSampler(proc_path=tmp_path / "x", resolver=_R(), owner_resolver=Owners())
    assert s.label_for("149.154.166.111") == ("Telegram Messenger Inc", "owner")
    assert s.label_for("71.18.93.236") == ("71.18.93.236", "ip")
    flows.DNS_NAMES.put("142.250.1.1", "www.google.com")
    assert s.label_for("142.250.1.1") == ("google.com", "name")


def test_seed_dns_names_from_log(tmp_path):
    log = tmp_path / "q.log"
    log.write_text("Oct 1 dnsmasq[1]: reply api.example-seed.com is 151.101.1.69\n"
                   "Oct 1 dnsmasq[1]: query[A] x from 1.2.3.4\n")
    assert flows.seed_dns_names_from_log(str(log)) == 1
    assert flows.DNS_NAMES.get("151.101.1.69") == "api.example-seed.com"
    assert flows.seed_dns_names_from_log(str(tmp_path / "missing")) == 0
