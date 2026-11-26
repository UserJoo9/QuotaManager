import re

import pytest

from quota.lan_interface import LanSelection
from quota.dnsmasq_binding import (
    DnsmasqError,
    FragmentUpdate,
    DnsmasqPaths,
    probe_dhcp_binding,
    reconcile_dnsmasq,
)


GOOD_SOCKETS = (
    'UNCONN 0 0 0.0.0.0%enp10s0f1:67 0.0.0.0:* users:(("dnsmasq",pid=12,fd=3))\n'
    'UNCONN 0 0 [::]%enp10s0f1:547 [::]:* users:(("dnsmasq",pid=12,fd=4))\n'
)


class FakeDnsmasq:
    def __init__(self, tmp_path, interface="enp10s0f1", ipv6=True):
        self.conf_dir = tmp_path / "dnsmasq.d"
        self.conf_dir.mkdir()
        self.target = self.conf_dir / "quota-gateway.conf"
        self.target.write_text(f"interface={interface}\n")
        self.backups = tmp_path / "backups"
        self.calls = []
        self.ipv6 = ipv6
        self.active = True
        self.current_interface = interface
        self.socket_output = GOOD_SOCKETS.replace("enp10s0f1", interface)
        self.config_valid = True
        self.restart_fail_once = False
        self.bad_socket_after_restart = False
        self.restarts = 0

    @property
    def paths(self):
        return DnsmasqPaths(self.target, self.conf_dir, self.backups)

    def run(self, argv):
        self.calls.append(argv)
        if argv == ["dnsmasq", "--test"]:
            siblings = [p for p in self.conf_dir.glob("*.conf")
                        if p != self.target and p.name != "quota-ipv6.conf"]
            if not self.config_valid or siblings:
                return 1, "illegal repeated keyword"
            return 0, "syntax check OK"
        if argv == ["systemctl", "restart", "dnsmasq"]:
            if self.restart_fail_once and self.restarts == 0:
                self.restarts += 1
                return 1, "restart failed"
            self.active = self.config_valid
            if self.active:
                import re
                match = re.search(r"^interface=(\S+)", self.target.read_text(), re.M)
                if match:
                    self.current_interface = match.group(1)
                self.socket_output = GOOD_SOCKETS.replace(
                    "enp10s0f1", self.current_interface
                )
                if self.bad_socket_after_restart:
                    self.socket_output = GOOD_SOCKETS.replace(
                        "enp10s0f1", "eno2"
                    )
            return (0, "") if self.active else (1, "restart failed")
        if argv == ["systemctl", "is-active", "--quiet", "dnsmasq"]:
            return (0, "active") if self.active else (3, "inactive")
        if argv == ["ss", "-H", "-lunp"]:
            return 0, self.socket_output
        if argv == ["systemctl", "show", "dnsmasq", "--property=InvocationID", "--value"]:
            return 0, "new-invocation\n"
        if argv[:3] == ["journalctl", "-u", "dnsmasq"]:
            return 0, "DHCP, sockets bound exclusively to interface enp10s0f1\n"
        return 1, "unsupported command"


def _selection(name="enp10s0f1"):
    return LanSelection(name, 7, "gateway")


def test_same_ip_stale_interface_restarts(tmp_path):
    fake = FakeDnsmasq(tmp_path, "eno1")
    change = reconcile_dnsmasq(
        _selection(), replacement=None, ipv6_fragment=None, expect_ipv6=True,
        force_restart=False, paths=fake.paths, run=fake.run,
    )
    assert "interface=enp10s0f1" in fake.target.read_text()
    assert ["systemctl", "restart", "dnsmasq"] in fake.calls
    assert change.binding.ipv4_interface == "enp10s0f1"
    assert change.binding.ipv6_interface == "enp10s0f1"


def test_matching_live_binding_is_noop(tmp_path):
    fake = FakeDnsmasq(tmp_path)
    change = reconcile_dnsmasq(
        _selection(), replacement=None, ipv6_fragment=None, expect_ipv6=True,
        force_restart=False, paths=fake.paths, run=fake.run,
    )
    assert change.changed is False
    assert ["systemctl", "restart", "dnsmasq"] not in fake.calls


def test_matching_file_stale_socket_restarts(tmp_path):
    fake = FakeDnsmasq(tmp_path)
    fake.current_interface = "eno1"
    fake.socket_output = GOOD_SOCKETS.replace("enp10s0f1", "eno1")
    reconcile_dnsmasq(
        _selection(), replacement=None, ipv6_fragment=None, expect_ipv6=True,
        force_restart=False, paths=fake.paths, run=fake.run,
    )
    assert ["systemctl", "restart", "dnsmasq"] in fake.calls


def test_effective_config_rejects_sibling_conflict(tmp_path):
    fake = FakeDnsmasq(tmp_path)
    (fake.conf_dir / "old-backup.conf").write_text("interface=eno1\n")
    with pytest.raises(DnsmasqError, match="validat"):
        reconcile_dnsmasq(
            _selection(), replacement=None, ipv6_fragment=None,
            expect_ipv6=True, force_restart=True, paths=fake.paths,
            run=fake.run,
        )
    assert ["systemctl", "restart", "dnsmasq"] not in fake.calls


def test_restart_or_wrong_socket_restores_file(tmp_path):
    fake = FakeDnsmasq(tmp_path, "eno1")
    fake.restart_fail_once = True
    with pytest.raises(DnsmasqError, match="restart"):
        reconcile_dnsmasq(
            _selection(), replacement=None, ipv6_fragment=None,
            expect_ipv6=True, force_restart=False, paths=fake.paths,
            run=fake.run,
        )
    assert fake.target.read_text() == "interface=eno1\n"
    assert not list(fake.conf_dir.glob("*.bak*"))
    assert fake.current_interface == "eno1"


def test_wrong_socket_after_restart_restores_file(tmp_path):
    fake = FakeDnsmasq(tmp_path, "eno1")
    fake.bad_socket_after_restart = True
    with pytest.raises(DnsmasqError, match="verification"):
        reconcile_dnsmasq(
            _selection(), replacement=None, ipv6_fragment=None,
            expect_ipv6=True, force_restart=False, paths=fake.paths,
            run=fake.run,
        )
    assert fake.target.read_text() == "interface=eno1\n"
    assert fake.current_interface == "eno1"


def test_ipv6_fragment_is_staged_with_interface_and_effective_config(tmp_path):
    fake = FakeDnsmasq(tmp_path, "eno1")
    ipv6 = fake.conf_dir / "quota-ipv6.conf"
    ipv6.write_text("old-ra\n")
    fragment = FragmentUpdate(ipv6, "enable-ra\ndhcp-range=fd00::100,fd00::ffff,slaac\n")
    change = reconcile_dnsmasq(
        _selection(), replacement=None, ipv6_fragment=fragment,
        expect_ipv6=True, force_restart=False, paths=fake.paths, run=fake.run,
    )
    assert "interface=enp10s0f1" in fake.target.read_text()
    assert ipv6.read_text().startswith("enable-ra")
    assert fake.calls.index(["dnsmasq", "--test"]) < \
        fake.calls.index(["systemctl", "restart", "dnsmasq"])
    assert change.binding.healthy


def test_legacy_dnsmasq_backups_move_out_of_conf_dir(tmp_path):
    fake = FakeDnsmasq(tmp_path, "enp10s0f1")
    legacy = fake.conf_dir / "quota-gateway.conf.bak-old"
    legacy.write_text("interface=eno1\n")
    change = reconcile_dnsmasq(
        _selection(), replacement=None, ipv6_fragment=None, expect_ipv6=True,
        force_restart=False, paths=fake.paths, run=fake.run,
    )
    assert change.quarantined == 1
    assert not legacy.exists()
    assert list(fake.backups.iterdir())


def test_v6_off_requires_only_v4(tmp_path):
    fake = FakeDnsmasq(tmp_path, ipv6=False)
    fake.socket_output = GOOD_SOCKETS.splitlines()[0] + "\n"
    result = probe_dhcp_binding(run=fake.run, expect_ipv6=False,
                                expected_interface="enp10s0f1")
    assert result.healthy
    assert result.ipv4_interface == "enp10s0f1"
    assert result.ipv6_interface == ""


def test_legacy_backups_quarantined_once(tmp_path):
    fake = FakeDnsmasq(tmp_path)
    fake.current_interface = "eno1"
    old = fake.conf_dir / "quota-gateway.conf.bak-old"
    old.write_text("stale backup\n")
    result = reconcile_dnsmasq(
        _selection(), replacement=None, ipv6_fragment=None, expect_ipv6=True,
        force_restart=False, paths=fake.paths, run=fake.run,
    )
    assert result.quarantined == 1
    assert not old.exists()
    quarantined = list(fake.backups.glob(f"{old.name}.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "stale backup\n"
    assert not re.search(r"\.bak", " ".join(p.name for p in fake.conf_dir.iterdir()))


def test_failed_apply_keeps_legacy_backup_quarantined(tmp_path):
    fake = FakeDnsmasq(tmp_path, "eno1")
    fake.config_valid = False
    legacy = fake.conf_dir / "quota-gateway.conf.bak-old"
    legacy.write_text("interface=eno1\n")

    with pytest.raises(DnsmasqError, match="validat"):
        reconcile_dnsmasq(
            _selection(), replacement=None, ipv6_fragment=None,
            expect_ipv6=True, force_restart=True, paths=fake.paths,
            run=fake.run,
        )

    assert not legacy.exists()
    assert len(list(fake.backups.glob("quota-gateway.conf.bak-old.*"))) == 1
