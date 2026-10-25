"""Tests for the self-applying IPv6 host config (no manual setup-script re-run)."""

from __future__ import annotations

from pathlib import Path

from quota.ipv6_setup import (box_uses_ula_dns, disable_ipv6_host_config,
                              dnsmasq_fragment, ensure_box_ignores_ula_dns, rollback_if_dnsmasq_down,
                              ensure_ipv6_host_config, sysctl_fragment)

PREFIX, GW = "fd00:da7a:2::/64", "fd00:da7a:2::1"


class FakeRun:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.code = code

    def __call__(self, argv):
        self.calls.append(argv)
        return self.code, ""


def _proc(tmp_path: Path, all_off: str = "0") -> Path:
    root = tmp_path / "proc"
    (root / "all").mkdir(parents=True, exist_ok=True)
    (root / "all" / "disable_ipv6").write_text(all_off + "\n")
    return root


def test_fragment_contents():
    assert dnsmasq_fragment(PREFIX, GW) == (
        "# Quota Manager — IPv6 dual-stack (generated, do not edit by hand).\n"
        "enable-ra\n"
        "dhcp-range=fd00:da7a:2::100,fd00:da7a:2::ffff,slaac,ra-names,12h\n"
        "dhcp-option=option6:dns-server,[fd00:da7a:2::1]\n")
    s = sysctl_fragment("eth0")
    assert "net.ipv6.conf.all.disable_ipv6 = 0" in s
    assert "net.ipv6.conf.eth0.accept_ra = 2" in s


def test_first_run_writes_both_and_applies_sysctl(tmp_path):
    run = FakeRun()
    dns, sysc = tmp_path / "quota-ipv6.conf", tmp_path / "99-zz.conf"
    res = ensure_ipv6_host_config(prefix=PREFIX, gateway=GW, iface="eth0", run=run,
                                  dnsmasq_path=dns, sysctl_path=sysc,
                                  proc_root=_proc(tmp_path))
    assert res == {"dnsmasq_changed": True, "sysctl_applied": True}
    assert dns.read_text(encoding="utf-8") == dnsmasq_fragment(PREFIX, GW)
    assert run.calls == [["sysctl", "-p", str(sysc)]]


def test_second_run_is_a_no_op(tmp_path):
    """Idempotent: nothing rewritten, dnsmasq NOT restarted, sysctl untouched."""
    kw = dict(prefix=PREFIX, gateway=GW, iface="eth0",
              dnsmasq_path=tmp_path / "d.conf", sysctl_path=tmp_path / "s.conf",
              proc_root=_proc(tmp_path))
    ensure_ipv6_host_config(run=FakeRun(), **kw)
    run = FakeRun()
    res = ensure_ipv6_host_config(run=run, **kw)
    assert res == {"dnsmasq_changed": False, "sysctl_applied": False}
    assert run.calls == []


def test_live_disabled_ipv6_is_re_enabled_even_if_files_exist(tmp_path):
    """An old install: files are fine but the kernel still has IPv6 off (the
    old setup script's disable_ipv6=1 won at boot)."""
    kw = dict(prefix=PREFIX, gateway=GW, iface="eth0",
              dnsmasq_path=tmp_path / "d.conf", sysctl_path=tmp_path / "s.conf")
    ensure_ipv6_host_config(run=FakeRun(), proc_root=_proc(tmp_path), **kw)
    run = FakeRun()
    res = ensure_ipv6_host_config(run=run, proc_root=_proc(tmp_path, "1"), **kw)
    assert res["sysctl_applied"] is True and run.calls


def test_bad_prefix_and_unwritable_paths_never_raise(tmp_path):
    assert ensure_ipv6_host_config(prefix="not-a-prefix", gateway=GW,
                                   run=FakeRun()) == {
        "dnsmasq_changed": False, "sysctl_applied": False}
    blocker = tmp_path / "file"
    blocker.write_text("x")
    res = ensure_ipv6_host_config(prefix=PREFIX, gateway=GW, run=FakeRun(1),
                                  dnsmasq_path=blocker / "d.conf",
                                  sysctl_path=blocker / "s.conf",
                                  proc_root=_proc(tmp_path))
    assert res["dnsmasq_changed"] is False


def test_disable_undoes_our_fragments_only(tmp_path):
    kw = dict(dnsmasq_path=tmp_path / "d.conf", sysctl_path=tmp_path / "s.conf")
    # never enabled -> untouched, nothing run
    run = FakeRun()
    assert disable_ipv6_host_config(run=run, **kw) == {
        "dnsmasq_changed": False, "sysctl_applied": False} and run.calls == []
    ensure_ipv6_host_config(prefix=PREFIX, gateway=GW, iface="eth0", run=FakeRun(),
                            proc_root=_proc(tmp_path), **kw)
    run = FakeRun()
    res = disable_ipv6_host_config(run=run, **kw)
    assert res == {"dnsmasq_changed": True, "sysctl_applied": True}
    assert not (tmp_path / "d.conf").exists()
    assert "all.disable_ipv6 = 1" in (tmp_path / "s.conf").read_text()
    assert disable_ipv6_host_config(run=FakeRun(), **kw)["sysctl_applied"] is False


class ScriptRun:
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv):
        self.calls.append(argv)
        return self.table.get(argv[0], (0, ""))


def test_detects_ula_nameserver_on_the_box(tmp_path):
    rc = tmp_path / "resolv.conf"
    rc.write_text("nameserver 192.168.1.1\nnameserver fd00:da7a:2::1\n")
    assert box_uses_ula_dns(PREFIX, resolv_path=rc, run=ScriptRun({"resolvectl": (1, "")}))
    rc.write_text("nameserver 192.168.1.1\nnameserver 2001:4860:4860::8888\n")
    assert not box_uses_ula_dns(PREFIX, resolv_path=rc, run=ScriptRun({"resolvectl": (1, "")}))


def test_resolved_link_dns_is_detected(tmp_path):
    rc = tmp_path / "resolv.conf"
    rc.write_text("nameserver 127.0.0.53\n")
    run = ScriptRun({"resolvectl": (0, "Link 2 (eth0)\n DNS Servers: fd00:da7a:2::1 1.1.1.1\n")})
    assert box_uses_ula_dns(PREFIX, resolv_path=rc, run=run, iface="eth0")


def test_box_dns_fix_uses_nmcli_and_is_idempotent(tmp_path):
    rc = tmp_path / "resolv.conf"
    rc.write_text("nameserver fd00:da7a:2::1\n")
    run = ScriptRun({"resolvectl": (1, ""), "nmcli": (0, "Wired 1:eth0\nlo:lo\n")})
    assert ensure_box_ignores_ula_dns(prefix=PREFIX, iface="eth0", run=run,
                                      resolv_path=rc) is True
    cmds = [c for c in run.calls if c[0] == "nmcli"]
    assert ["nmcli", "con", "mod", "Wired 1", "ipv6.ignore-auto-dns", "yes"] in cmds
    assert ["nmcli", "device", "reapply", "eth0"] in cmds
    rc.write_text("nameserver 192.168.1.1\n")           # fixed -> no-op
    run2 = ScriptRun({"resolvectl": (1, "")})
    assert ensure_box_ignores_ula_dns(prefix=PREFIX, iface="eth0", run=run2,
                                      resolv_path=rc) is False
    assert not [c for c in run2.calls if c[0] == "nmcli"]


def test_empty_resolver_gets_the_upstream_dns(tmp_path):
    """The reported case: resolv.conf has no nameserver at all, so the VPN client
    on the box queries ::1 and is refused. The upstream DNS is set via nmcli."""
    rc = tmp_path / "resolv.conf"
    rc.write_text("# Generated by NetworkManager\n")
    run = ScriptRun({"resolvectl": (1, ""), "nmcli": (0, "Wired connection 1:eth0\n")})
    assert ensure_box_ignores_ula_dns(prefix=PREFIX, iface="eth0", run=run, resolv_path=rc,
                                      fallback_dns=["192.168.1.1", "8.8.8.8", "::1"])
    assert ["nmcli", "con", "mod", "Wired connection 1", "ipv4.dns",
            "192.168.1.1 8.8.8.8"] in run.calls


def test_loopback_only_resolver_counts_as_empty_but_real_one_does_not(tmp_path):
    rc = tmp_path / "resolv.conf"
    rc.write_text("nameserver ::1\nnameserver 127.0.0.1\n")
    assert not __import__("quota.ipv6_setup", fromlist=["x"]).box_has_usable_dns(
        PREFIX, resolv_path=rc)
    rc.write_text("nameserver 192.168.1.1\n")
    assert ensure_box_ignores_ula_dns(prefix=PREFIX, iface="eth0",
                                      run=ScriptRun({"resolvectl": (1, "")}),
                                      resolv_path=rc, fallback_dns=["8.8.8.8"]) is False


def test_fragment_never_conflicts_with_bind_interfaces():
    """Regression: `bind-dynamic` next to the main conf's `bind-interfaces` made
    dnsmasq refuse to start ("cannot set --bind-interfaces and --bind-dynamic")."""
    assert "bind-" not in dnsmasq_fragment(PREFIX, GW)


def test_rollback_when_dnsmasq_is_down(tmp_path):
    frag = tmp_path / "q.conf"
    frag.write_text("enable-ra\n")
    down = ScriptRun({"systemctl": (3, "")})
    assert rollback_if_dnsmasq_down(run=down, dnsmasq_path=frag) is True
    assert not frag.exists()
    assert ["systemctl", "restart", "dnsmasq"] in down.calls
    frag.write_text("enable-ra\n")                      # running -> untouched
    assert rollback_if_dnsmasq_down(run=ScriptRun({}), dnsmasq_path=frag) is False
    assert frag.exists()
