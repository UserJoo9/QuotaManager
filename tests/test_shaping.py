"""Tests for the tc speed-shaping engine (Linux), using a fake ``tc``/``ip``/
``modprobe`` runner so no root or kernel features are required.

The fake records every argv, mirroring :class:`FakeNft` in test_nftables.py —
we assert the command sequence that programs the kernel, not kernel behavior.
"""

from __future__ import annotations

from core.config import Config
from quota.shaping import TcShaper, _derive_client_subnet, _effective


def make_cfg(iface: str = "eth0", client_subnet: str = "192.168.2.0/24",
             gateway_ip: str = "192.168.2.1", ifb: str = "ifb0") -> Config:
    """A Config whose shaping block is fully specified (no auto-detect)."""
    cfg = Config()
    cfg.dhcp.gateway_ip = gateway_ip
    cfg.dhcp.subnet = "255.255.255.0"
    cfg.shaping.interface = iface
    cfg.shaping.client_subnet = client_subnet
    cfg.shaping.ifb = ifb
    return cfg


class FakeTc:
    """In-memory stand-in for ``tc``/``ip``/``modprobe``.

    ``__call__`` takes the full argv like a subprocess would. The default
    behavior is success (returncode 0); tests can force failures per binary.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_binary: str | None = None  # e.g. "tc" to fail every tc call
        self.fail_code = 1

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if self.fail_binary and argv[0] == self.fail_binary:
            return self.fail_code, f"{self.fail_binary}: simulated failure"
        return 0, ""

    def count(self, prefix: str, *tail: str) -> int:
        """How many calls match the given argv prefix (e.g. ``count("tc")``)."""
        n = 0
        for argv in self.calls:
            if len(argv) >= len(tail) + 1 and argv[0] == prefix:
                if argv[1:1 + len(tail)] == list(tail):
                    n += 1
        return n

    def has(self, *argv: str) -> bool:
        return any(a[:len(argv)] == list(argv) for a in self.calls)


def entry(ip: str, device_id: int, user_id: int, down: float = 0.0,
          up: float = 0.0, user_down: float = 0.0,
          user_up: float = 0.0) -> dict:
    """One rate_map entry (the shape run.py feeds update_state)."""
    return {"ip": ip, "device_id": device_id, "user_id": user_id,
            "down": down, "up": up, "user_down": user_down, "user_up": user_up}


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def test_effective_math():
    assert _effective(0, 0, 100) is None              # unlimited
    assert _effective(10, 0, 100) == 10                # device only
    assert _effective(0, 20, 100) == 20                # user only
    assert _effective(10, 20, 100) == 10               # min(dev, user)
    assert _effective(10, 5, 100) == 5
    assert _effective(10, 0, 3) == 3                   # clamped to line total
    assert _effective(0, 500, 50) == 50                # user > line total


def test_derive_client_subnet():
    assert _derive_client_subnet("192.168.2.1", "255.255.255.0") == "192.168.2.0/24"
    assert _derive_client_subnet("192.168.2.1", "") == "192.168.2.0/24"
    assert _derive_client_subnet("", "255.255.255.0") == ""


# ---------------------------------------------------------------------------
# build: the full two-tree program sequence
# ---------------------------------------------------------------------------

def test_full_build_emits_expected_tree():
    fake = FakeTc()
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    shaper.update_state(
        [entry("192.168.2.100", device_id=1, user_id=1, down=10, up=5)],
        True, 100.0, 20.0, True)

    # probe + ifb bring-up: start() probes ifb0 and brings it up. Since the
    # fake's ifb0 already exists, start() never modprobes — and the APPLY no
    # longer re-runs modprobe either (a no-op `modprobe ifb numifbs=1` at
    # apply time is what killed the shaper on a box with no ifb0).
    assert fake.has("tc", "-V")
    assert fake.has("ip", "link", "show", "dev", "ifb0")
    assert fake.has("ip", "link", "set", "dev", "ifb0", "up")
    assert not fake.has("modprobe", "ifb", "numifbs=1")
    # ingress redirect of the client subnet into ifb0
    assert fake.has("tc", "qdisc", "add", "dev", "eth0", "handle", "ffff:", "ingress")
    assert fake.has("tc", "filter", "add", "dev", "eth0", "parent", "ffff:",
                    "protocol", "ip", "u32", "match", "ip", "src",
                    "192.168.2.0/24", "action", "mirred", "egress", "redirect",
                    "dev", "ifb0")
    # two HTB trees: download on eth0 egress, upload on ifb0 egress
    assert fake.has("tc", "qdisc", "add", "dev", "eth0", "root", "handle", "1:",
                    "htb", "default", "2")
    assert fake.has("tc", "qdisc", "add", "dev", "ifb0", "root", "handle", "1:",
                    "htb", "default", "2")
    # class ids: root 1:1, default 1:2, download aggregate 1:100, user 1:0x301,
    # device leaf 1:0x8001 (devid 1)
    assert fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:", "classid",
                    "1:1", "htb", "rate", "100mbit")
    assert fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:1", "classid",
                    "1:2", "htb", "rate", "100mbit", "ceil", "100mbit")
    assert fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:1", "classid",
                    "1:100", "htb", "rate", "100mbit", "ceil", "100mbit")
    assert fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:100",
                    "classid", "1:0x301", "htb", "rate", "100mbit",
                    "ceil", "100mbit")
    assert fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:0x301",
                    "classid", "1:0x8001", "htb", "rate", "10mbit",
                    "ceil", "10mbit")
    # fq_codel on the default class and on the device leaf (AQM on)
    assert fake.has("tc", "qdisc", "add", "dev", "eth0", "parent", "1:2",
                    "handle", "2:", "fq_codel")
    assert fake.has("tc", "qdisc", "add", "dev", "eth0", "parent", "1:0x8001",
                    "handle", "0x8001:", "fq_codel")
    # per-device filters: dst on eth0 (download), src on ifb0 (upload)
    assert fake.has("tc", "filter", "add", "dev", "eth0", "parent", "1:",
                    "protocol", "ip", "prio", "1", "u32", "match", "ip", "dst",
                    "192.168.2.100", "flowid", "1:0x8001")
    assert fake.has("tc", "filter", "add", "dev", "ifb0", "parent", "1:",
                    "protocol", "ip", "prio", "1", "u32", "match", "ip", "src",
                    "192.168.2.100", "flowid", "1:0x8001")
    # per-device caps on the upload tree use the device UP cap (5mbit)
    assert fake.has("tc", "class", "add", "dev", "ifb0", "parent", "1:0x301",
                    "classid", "1:0x8001", "htb", "rate", "5mbit", "ceil", "5mbit")


def test_unlimited_device_goes_to_default_class():
    fake = FakeTc()
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    shaper.update_state(
        [entry("192.168.2.101", device_id=2, user_id=1, down=0, up=0)],
        True, 100.0, 20.0, True)
    # no user class / device leaf / filter: only 1:1, 1:2, 1:100 on EACH tree
    assert fake.count("tc", "class", "add") == 6  # 3 per tree (eth0 + ifb0)
    assert not fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:0x301",
                        "classid", "1:0x8002")
    assert not fake.has("tc", "class", "add", "dev", "ifb0", "parent", "1:0x301",
                        "classid", "1:0x8002")
    # only the ingress-redirect filter exists — no per-device flowid filters
    assert fake.count("tc", "filter", "add", "dev", "eth0", "parent", "ffff:") == 1
    assert fake.count("tc", "filter", "add", "dev", "ifb0") == 0
    assert fake.count("tc", "filter", "add", "dev", "eth0", "parent", "1:") == 0


def test_device_up_cap_honored_when_down_unlimited():
    """A device with ONLY an upload cap (up>0, down=0) and a user with no
    upload aggregate still gets an upload leaf. The leaves filter used the DOWN
    cap in both directions, so an up-only device was dropped from the upload
    tree — its upload limit was silently unenforced (a live box report)."""
    fake = FakeTc()
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    shaper.update_state(
        [entry("192.168.2.100", device_id=1, user_id=1, down=0, up=5)],
        True, 100.0, 20.0, True)
    # upload tree (ifb0): the device gets a leaf at its UP cap, not dropped
    assert fake.has("tc", "class", "add", "dev", "ifb0", "parent", "1:0x301",
                    "classid", "1:0x8001", "htb", "rate", "5mbit",
                    "ceil", "5mbit")
    assert fake.has("tc", "filter", "add", "dev", "ifb0", "parent", "1:",
                    "protocol", "ip", "prio", "1", "u32", "match", "ip", "src",
                    "192.168.2.100", "flowid", "1:0x8001")
    # download tree (eth0): the device has no DOWN cap -> default class only
    assert not fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:0x301",
                        "classid", "1:0x8001")


def test_user_aggregate_caps_device_leaves():
    fake = FakeTc()
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    # user 1 caps at 30 down / 10 up; two devices share that user class
    shaper.update_state(
        [entry("192.168.2.100", device_id=1, user_id=1, down=10, up=5,
               user_down=30, user_up=10),
         entry("192.168.2.101", device_id=2, user_id=1, down=15, up=0,
               user_down=30, user_up=10)],
        True, 100.0, 20.0, True)
    # user class capped at the user's aggregate (30 down / 10 up)
    assert fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:100",
                    "classid", "1:0x301", "htb", "rate", "30mbit", "ceil", "30mbit")
    assert fake.has("tc", "class", "add", "dev", "ifb0", "parent", "1:100",
                    "classid", "1:0x301", "htb", "rate", "10mbit", "ceil", "10mbit")
    # second device: up=0 (unlimited on upload) but the USER's 10 up still
    # caps it -> it gets an upload leaf at 10mbit (min(∞, user cap)).
    assert fake.has("tc", "class", "add", "dev", "ifb0", "parent", "1:0x301",
                    "classid", "1:0x8002", "htb", "rate", "10mbit",
                    "ceil", "10mbit")


def test_every_class_has_tight_burst():
    """HTB's default token bucket is ~1 second of traffic at the class rate,
    so a class can burst at full line speed for up to a second — a short speed
    test then reads ~1.5x the configured cap. Every class must carry an
    explicit small burst/cburst (rate/20, floored at one frame) that is large
    enough to sustain the rate but too small to measurably overshoot it."""
    fake = FakeTc()
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    shaper.update_state(
        [entry("192.168.2.100", device_id=1, user_id=1, down=2, up=2)],
        True, 100.0, 20.0, True)
    classes = [a for a in fake.calls if a[:3] == ["tc", "class", "add"]]
    assert classes, "the two HTB trees must create classes"
    for argv in classes:
        assert "burst" in argv and "cburst" in argv, \
            f"class missing burst/cburst: {argv}"
        burst = int(argv[argv.index("burst") + 1])
        cburst = int(argv[argv.index("cburst") + 1])
        assert burst == cburst >= 1500, f"tiny/negative burst: {argv}"
    # 2 Mbps leaf -> rate/20 = 250 KB/s / 20 = 12.5 KB (not 1s = 250 KB)
    assert fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:0x301",
                    "classid", "1:0x8001", "htb", "rate", "2mbit",
                    "ceil", "2mbit", "burst", "12500", "cburst", "12500")
    # 100 Mbps root/default/aggregate -> 12.5 MB/s / 20 = 625 KB
    assert fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:",
                    "classid", "1:1", "htb", "rate", "100mbit",
                    "burst", "625000", "cburst", "625000")


def test_aqm_off_no_fq_codel():
    fake = FakeTc()
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    shaper.update_state(
        [entry("192.168.2.100", device_id=1, user_id=1, down=10, up=5)],
        True, 100.0, 20.0, False)
    assert fake.count("tc", "qdisc", "add") == 3  # ingress + 2 root HTB, no fq_codel
    assert not fake.has("tc", "qdisc", "add", "dev", "eth0", "parent", "1:2",
                        "handle", "2:", "fq_codel")


def test_disabled_or_zero_totals_teardown_only():
    for kwargs in ({"enabled": False},
                   {"total_down": 0.0},
                   {"total_up": 0.0}):
        fake = FakeTc()
        shaper = TcShaper(make_cfg(), run_command=fake)
        shaper.start()
        shaper.update_state([entry("192.168.2.100", 1, 1)],
                            kwargs.get("enabled", True),
                            kwargs.get("total_down", 100.0),
                            kwargs.get("total_up", 20.0), True)
        # teardown del calls happened, no add-class/htb program
        assert fake.count("tc", "qdisc", "del") >= 2
        assert fake.count("tc", "class", "add") == 0


def test_signature_no_rebuild_on_unchanged_state():
    fake = FakeTc()
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    state = dict(enabled=True, total_down=100.0, total_up=20.0, aqm=True)
    rm = [entry("192.168.2.100", 1, 1, down=10, up=5)]
    shaper.update_state(rm, **state)
    before = len(fake.calls)
    # identical state again -> the maintenance loop calls update_state every tick
    shaper.update_state(rm, **state)
    assert len(fake.calls) == before
    # a changed cap DOES rebuild
    shaper.update_state([entry("192.168.2.100", 1, 1, down=25, up=5)], **state)
    assert len(fake.calls) > before


def test_missing_tc_degrades_without_raise():
    fake = FakeTc()
    fake.fail_binary = "tc"  # `tc -V` fails -> start() degrades
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    assert shaper.available is False
    # update_state must not raise even when unavailable
    shaper.update_state([entry("192.168.2.100", 1, 1, down=10, up=5)],
                        True, 100.0, 20.0, True)


class _ModprobeFailsNoIfb(FakeTc):
    """modprobe fails and there is no ifb0 — the shaper cannot work at all."""

    def __call__(self, argv):  # noqa: D102
        self.calls.append(argv)
        if argv == ["ip", "link", "show", "dev", "ifb0"]:
            return 1, "Cannot find device ifb0"
        if argv[0] == "modprobe":
            return 1, "modprobe: FATAL: Module ifb not found"
        return 0, ""


def test_modprobe_failure_degrades():
    fake = _ModprobeFailsNoIfb()
    shaper = TcShaper(make_cfg(), run_command=fake)
    # ifb0 is a hard prerequisite (uploads redirect into it), so a missing
    # module degrades at start() — the old code only discovered it at the
    # first apply, after a start() that looked fine.
    shaper.start()
    assert shaper.available is False
    # update_state must not raise even when unavailable
    shaper.update_state([entry("192.168.2.100", 1, 1, down=10, up=5)],
                        True, 100.0, 20.0, True)


class _IfbNoOpFirstLoad(FakeTc):
    """The box bug: the first ``modprobe ifb numifbs=1`` returns 0 but creates
    no ifb0 (the module was already loaded with a different numifbs, so kmod
    no-ops instead of re-creating the netdev); only a load AFTER an unload
    actually creates the device."""

    def __init__(self) -> None:
        super().__init__()
        self.loads = 0
        self.ifb0_exists = False

    def __call__(self, argv):  # noqa: D102
        self.calls.append(argv)
        if argv == ["ip", "link", "show", "dev", "ifb0"]:
            return (0 if self.ifb0_exists else 1), "Cannot find device ifb0"
        if argv == ["modprobe", "ifb", "numifbs=1"]:
            self.loads += 1
            if self.loads > 1:  # a clean load (after `-r`) creates ifb0
                self.ifb0_exists = True
            return 0, ""
        if argv == ["modprobe", "-r", "ifb"]:
            self.ifb0_exists = False
            return 0, ""
        return 0, ""


def test_missing_ifb0_forces_clean_reload():
    """A live box report: per-device / per-user speed limits did nothing because
    ``modprobe ifb numifbs=1`` returned 0 but no ifb0 existed, so the apply's
    ``ip link set dev ifb0 up`` failed and permanently degraded the shaper.
    start() must notice the missing device, unload + reload cleanly, and then
    shape normally."""
    fake = _IfbNoOpFirstLoad()
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    assert shaper.available is True
    # the missing ifb0 triggered an unload + a second load, verified, then up
    assert fake.count("modprobe", "-r", "ifb") == 1
    assert fake.count("modprobe", "ifb", "numifbs=1") == 2
    assert fake.has("ip", "link", "set", "dev", "ifb0", "up")
    # shaping then programs the trees normally
    shaper.update_state([entry("192.168.2.100", 1, 1, down=10, up=5)],
                        True, 100.0, 20.0, True)
    assert fake.has("tc", "class", "add", "dev", "eth0", "parent", "1:0x301",
                    "classid", "1:0x8001", "htb", "rate", "10mbit",
                    "ceil", "10mbit")


class _IfbNeverAppears(FakeTc):
    """modprobe succeeds but no ifb0 is ever created (module unavailable)."""

    def __call__(self, argv):  # noqa: D102
        self.calls.append(argv)
        if argv == ["ip", "link", "show", "dev", "ifb0"]:
            return 1, "Cannot find device ifb0"
        return 0, ""


def test_ifb0_never_created_degrades_at_start():
    fake = _IfbNeverAppears()
    shaper = TcShaper(make_cfg(), run_command=fake)
    shaper.start()
    assert shaper.available is False
    # update_state must not raise even when unavailable
    shaper.update_state([entry("192.168.2.100", 1, 1, down=10, up=5)],
                        True, 100.0, 20.0, True)


def test_auto_detect_interface():
    fake = FakeTc()
    fake.calls.append(["ip", "-o", "-4", "addr", "show"])
    fake.calls.append(["ip", "-o", "-4", "addr", "show"])  # __call__ not used yet
    # The auto-detect helper parses `ip -o -4 addr show` output directly.
    from quota.shaping import _find_interface_for

    def ip_addr(argv):
        if argv == ["ip", "-o", "-4", "addr", "show"]:
            return 0, ("2: eth0    inet 192.168.1.110/24 brd 192.168.1.255 scope global eth0\n"
                       "2: eth0    inet 192.168.2.1/24 brd 192.168.2.255 scope global eth0\n")
        return 0, ""

    assert _find_interface_for("192.168.2.1", ip_addr) == "eth0"
    assert _find_interface_for("192.168.9.9", ip_addr) == ""
