from pathlib import Path

from quota.ppp_binding import reconcile_ppp_interface


class FakeSystemd:
    def __init__(self, fail_restart=False):
        self.calls = []
        self.fail_restart = fail_restart

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "restart"] and self.fail_restart:
            self.fail_restart = False
            return 1, "restart failed"
        return 0, "active"


def _files(tmp_path: Path):
    peer = tmp_path / "quota-wan"
    peer.write_text("# peer\nplugin pppoe.so\neno1\npersist\n")
    service = tmp_path / "quota-wan-ppp.service"
    service.write_text("Description=Quota Manager PPPoE WAN (dial eno1 -> ppp0)\n")
    return peer, service


def test_updates_peer_and_unit_for_single_nic_wan(tmp_path):
    peer, service = _files(tmp_path)
    runner = FakeSystemd()
    assert reconcile_ppp_interface(wan_interface="enp10s0f1", run=runner,
                                   peer_path=peer, service_path=service)
    assert "\nenp10s0f1\n" in peer.read_text()
    assert "dial enp10s0f1 -> ppp0" in service.read_text()
    assert ["systemctl", "daemon-reload"] in runner.calls
    assert ["systemctl", "restart", "quota-wan-ppp"] in runner.calls


def test_preserves_dedicated_wan_interface(tmp_path):
    peer, service = _files(tmp_path)
    assert reconcile_ppp_interface(wan_interface="enp2s0", run=FakeSystemd(),
                                   peer_path=peer, service_path=service)
    assert "\nenp2s0\n" in peer.read_text()


def test_restart_failure_restores_peer_and_unit(tmp_path):
    peer, service = _files(tmp_path)
    old_peer, old_service = peer.read_text(), service.read_text()
    runner = FakeSystemd(fail_restart=True)
    assert not reconcile_ppp_interface(wan_interface="enp10s0f1", run=runner,
                                       peer_path=peer, service_path=service)
    assert peer.read_text() == old_peer
    assert service.read_text() == old_service
