"""WAN-topology detection for the dashboard WAN tab.

``detect_ppp`` reports whether a PPP link (ppp0) is up and what address pair it
carries — all reads go through an injected ``run_command`` + a ``sysfs_root``
so the tests need no real ``ip`` binary, no root, and no /sys (Windows dev box).
``check_internet`` probes public reachability through an injected ``connect`` so
the tests fake the network instead of dialing out. ``check_internet_dns`` is the
same idea via an injected ``query`` (raw UDP DNS, used while the box's own
internet is cut — UDP 53 is exempted from the gateway block).
"""

from __future__ import annotations

import socket

from quota.topology import check_internet, check_internet_dns, detect_ppp


def _mk_fake(responses: dict[str, tuple[int, str]]) -> callable:
    """A run_command that answers known argv -> (code, stdout) pairs."""
    def run(argv):
        return responses.get(tuple(argv), (1, ""))
    return run


def _sysfs_root(tmp_path, operstate: str) -> str:
    """A fake /sys/class/net/ppp0/operstate tree (the dev box has no /sys).

    detect_ppp reads ``<sysfs_root>/ppp0/operstate``, so the returned root is
    the ``net`` directory that contains ``ppp0/``.
    """
    root = tmp_path / "net" / "ppp0"
    root.mkdir(parents=True)
    (root / "operstate").write_text(operstate + "\n", encoding="utf-8")
    return str(tmp_path / "net")


def test_ppp_up_with_address_pair():
    """A dialed PPP link reports up with its local + peer addresses."""
    fake = _mk_fake({
        ("ip", "-o", "-4", "addr", "show", "ppp0"):
            (0, "7: ppp0    inet 100.64.0.2 peer 100.64.0.1/32 scope global ppp0\n"),
    })
    out = detect_ppp("ppp0", run_command=fake, sysfs_root="/sys/class/net")
    assert out["state"] == "up"
    assert out["local"] == "100.64.0.2"
    assert out["peer"] == "100.64.0.1"


def test_ppp_up_without_peer():
    """A link can be up with only a local address (no peer line)."""
    fake = _mk_fake({
        ("ip", "-o", "-4", "addr", "show", "ppp0"):
            (0, "7: ppp0    inet 10.0.0.2/32 scope global ppp0\n"),
    })
    out = detect_ppp("ppp0", run_command=fake, sysfs_root="/sys/class/net")
    assert out["state"] == "up"
    assert out["local"] == "10.0.0.2"
    assert out["peer"] == ""


def test_ppp_down_when_sysfs_says_down(tmp_path):
    """operstate no longer short-circuits (v19.8): a live ppp can report
    non-up operstate, so the answer comes from `ip`. An interface that exists
    but carries no IPv4 is down."""
    calls = []

    def run(argv):
        calls.append(argv)
        return (1, "")  # `ip` cannot show an address on the interface

    out = detect_ppp("ppp0", run_command=run,
                     sysfs_root=_sysfs_root(tmp_path, "down"))
    assert out["state"] == "down"
    assert calls != []  # ip IS consulted — operstate is not trusted for ppp


def test_ppp_unknown_when_unreadable():
    """No /sys entry and no `ip` => degrade to "unknown", never raise."""
    fake = _mk_fake({})  # every argv returns (1, "")
    out = detect_ppp("ppp0", run_command=fake, sysfs_root="/nonexistent/sys")
    assert out["state"] == "unknown"
    assert out["local"] == ""
    assert out["peer"] == ""


def test_ppp_present_but_no_ipv4_is_down():
    """`ip` lists the interface but it carries no IPv4 => down."""
    fake = _mk_fake({
        ("ip", "-o", "-4", "addr", "show", "ppp0"):
            (0, "7: ppp0    inet6 fe80::1/64 scope link\n"),
    })
    out = detect_ppp("ppp0", run_command=fake, sysfs_root="/sys/class/net")
    assert out["state"] == "down"


def test_ppp_up_even_when_sysfs_missing():
    """No sysfs entry but `ip` reports an address => up (not unknown)."""
    fake = _mk_fake({
        ("ip", "-o", "-4", "addr", "show", "ppp0"):
            (0, "7: ppp0    inet 100.64.0.2 peer 100.64.0.1/32 scope global ppp0\n"),
    })
    out = detect_ppp("ppp0", run_command=fake, sysfs_root="/nonexistent/sys")
    assert out["state"] == "up"
    assert out["local"] == "100.64.0.2"


def test_ppp_up_even_when_sysfs_operstate_unknown(tmp_path):
    """REGRESSION (v19.8 box report): ppp interfaces are carrier-less — the
    kernel reports operstate 'unknown' even while pppd is connected with a live
    address pair. A dialed-up line MUST read up, never down."""
    fake = _mk_fake({
        ("ip", "-o", "-4", "addr", "show", "ppp0"):
            (0, "7: ppp0    inet 197.121.113.253 peer 10.10.12.17/32 scope global ppp0\n"),
    })
    out = detect_ppp("ppp0", run_command=fake,
                     sysfs_root=_sysfs_root(tmp_path, "unknown"))
    assert out["state"] == "up"
    assert out["local"] == "197.121.113.253"
    assert out["peer"] == "10.10.12.17"


def test_ppp_up_even_when_sysfs_operstate_down(tmp_path):
    """Same regression for operstate 'down': the negotiated IP is the truth."""
    fake = _mk_fake({
        ("ip", "-o", "-4", "addr", "show", "ppp0"):
            (0, "7: ppp0    inet 197.121.113.253 peer 10.10.12.17/32 scope global ppp0\n"),
    })
    out = detect_ppp("ppp0", run_command=fake,
                     sysfs_root=_sysfs_root(tmp_path, "down"))
    assert out["state"] == "up"
    assert out["local"] == "197.121.113.253"
    assert out["peer"] == "10.10.12.17"


# ---------------------------------------------------------------------------
# check_internet — the WAN-tab green dot (fake `connect`, no real network)
# ---------------------------------------------------------------------------

def _mk_connect(reachable: set[str]):
    """A fake socket.create_connection: succeeds for ``reachable`` hosts only."""
    def connect(addr, timeout):
        if addr[0] in reachable:
            return socket.socket()
        raise OSError("network unreachable")
    return connect


def test_internet_up_when_any_host_connects():
    """True when the FIRST host accepts a TCP connection."""
    assert check_internet(hosts=("1.1.1.1", "8.8.8.8"),
                          connect=_mk_connect({"1.1.1.1"})) is True


def test_internet_down_when_no_host_connects():
    """False when every host is unreachable (no route / line down)."""
    assert check_internet(hosts=("1.1.1.1", "8.8.8.8"),
                          connect=_mk_connect(set())) is False


def test_internet_falls_through_to_second_host():
    """The first host failing does not stop the probe — the next is tried."""
    assert check_internet(hosts=("1.1.1.1", "8.8.8.8"),
                          connect=_mk_connect({"8.8.8.8"})) is True


def test_internet_uses_raw_ips_not_dns():
    """The probe connects by IP — a resolver failure must not false-negative."""
    def connect(addr, timeout):
        # If the probe ever tried to resolve a hostname, it would call a fake
        # "connect" with a name — which we treat as an unreachable host.
        if addr[0] == "1.1.1.1":
            return socket.socket()
        raise OSError("no such host")
    assert check_internet(hosts=("1.1.1.1",), connect=connect) is True


# check_internet_dns — the green dot's fallback while the box itself is cut
# (the gateway block exempts UDP 53, so DNS still proves the line delivers)
# ---------------------------------------------------------------------------

def test_internet_dns_up_when_any_server_answers():
    """True when ANY resolver answers the raw DNS query."""
    assert check_internet_dns(servers=("8.8.8.8", "1.1.1.1"),
                              query=lambda s, t: s == "1.1.1.1") is True


def test_internet_dns_down_when_no_server_answers():
    """False when every resolver is unreachable (line down / no route)."""
    assert check_internet_dns(servers=("8.8.8.8", "1.1.1.1"),
                              query=lambda s, t: False) is False


def test_internet_dns_falls_through_to_second_server():
    """The first resolver failing does not stop the probe — the next is tried."""
    assert check_internet_dns(servers=("8.8.8.8", "1.1.1.1"),
                              query=lambda s, t: s == "8.8.8.8") is True
