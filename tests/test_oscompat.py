"""OpenWrt compatibility helpers: service control mapping, tmpfs detection,
DB backup, and the systemctl shim script."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core import oscompat  # noqa: E402


def test_systemd_commands_are_unchanged():
    assert oscompat.service_cmd("restart", "dnsmasq", openwrt=False) == \
        ["systemctl", "restart", "dnsmasq"]
    assert oscompat.service_cmd("is-active", "dnsmasq", quiet=True, openwrt=False) == \
        ["systemctl", "is-active", "--quiet", "dnsmasq"]
    assert oscompat.service_cmd("enable", "tailscaled", extra=["--now"], openwrt=False) == \
        ["systemctl", "enable", "--now", "tailscaled"]


def test_openwrt_commands_use_init_d():
    assert oscompat.service_cmd("restart", "quota-gateway", openwrt=True) == \
        ["/etc/init.d/quota-manager", "restart"]
    assert oscompat.service_cmd("try-restart", "dnsmasq", openwrt=True) == \
        ["/etc/init.d/dnsmasq", "restart"]
    assert oscompat.service_cmd("is-active", "dnsmasq", openwrt=True) == \
        ["/etc/init.d/dnsmasq", "running"]


def test_is_openwrt_detects_release_file(tmp_path):
    assert not oscompat.is_openwrt(str(tmp_path))
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "openwrt_release").write_text("DISTRIB_ID='OpenWrt'\n")
    assert oscompat.is_openwrt(str(tmp_path))


def test_database_backup_keeps_newest_copies(tmp_path):
    from quota.db import Database
    p = tmp_path / "quota.db"
    c = sqlite3.connect(p)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    db = Database(p)
    out = None
    for i in range(4):
        # distinct names without sleeping: pre-seed older files
        (tmp_path / "backups").mkdir(exist_ok=True)
        (tmp_path / "backups" / f"quota-2000010{i}-000000.db").write_bytes(b"old")
    out = db.backup_sync(keep=3)
    names = sorted(os.listdir(tmp_path / "backups"))
    assert len(names) == 3 and Path(out).name in names
    assert sqlite3.connect(out).execute("SELECT x FROM t").fetchall() == [(1,)]


@pytest.mark.skipif(os.name != "posix", reason="shell shim")
def test_systemctl_shim_maps_to_init_d(tmp_path):
    shim = (REPO / "scripts" / "systemctl.openwrt").read_text()
    fake = tmp_path / "sc"
    fake.write_text(shim.replace("/etc/init.d/", str(tmp_path) + "/"))
    svc = tmp_path / "dnsmasq"
    svc.write_text('#!/bin/sh\necho "$1" > "$0.called"\n')
    svc.chmod(0o755)
    r = subprocess.run(["sh", str(fake), "--quiet", "restart", "dnsmasq.service"])
    assert r.returncode == 0
    assert (tmp_path / "dnsmasq.called").read_text().strip() == "restart"
    assert subprocess.run(["sh", str(fake), "daemon-reload"]).returncode == 0
    assert subprocess.run(["sh", str(fake), "bogus", "dnsmasq"],
                          capture_output=True).returncode != 0


def test_openwrt_installer_handles_pure_python_archs():
    text = (REPO / "scripts" / "install_openwrt.sh").read_text()
    assert "requirements-openwrt.txt" in text
    assert "systemctl.openwrt" in text
    assert (REPO / "requirements-openwrt.txt").exists()
    init = (REPO / "scripts" / "quota-manager.openwrt").read_text()
    assert "/overlay/quota-gateway" in init, "DB must live on persistent storage"
