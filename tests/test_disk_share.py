"""Share > Network drives: disks plugged into the gateway shared over SMB."""
from __future__ import annotations

import asyncio
import json

import pytest

from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota import disk_share as ds
from quota.engine import EngineSnapshot, SnapshotHolder
from quota.service import QuotaService

LSBLK = {"blockdevices": [
    {"name": "vda", "path": "/dev/vda", "type": "disk", "rm": False, "model": "QEMU",
     "size": 3221225472, "fstype": None, "uuid": None, "mountpoints": [None],
     "children": [
         {"name": "vda1", "path": "/dev/vda1", "type": "part", "fstype": "ext4",
          "uuid": "root-uuid-1", "label": "", "size": 3000000000, "mountpoints": ["/"]},
         {"name": "vda15", "path": "/dev/vda15", "type": "part", "fstype": "vfat",
          "uuid": "AB12-CD34", "label": "", "size": 127000000, "mountpoints": ["/boot/efi"]}]},
    {"name": "sda", "path": "/dev/sda", "type": "disk", "rm": True, "model": "WD Elements ",
     "size": 1000204886016, "fstype": None, "uuid": None, "mountpoints": [None],
     "children": [
         {"name": "sda1", "path": "/dev/sda1", "type": "part", "fstype": "ntfs",
          "uuid": "7A3C55D23C558B6F", "label": "Movies", "size": 1000202788864,
          "mountpoints": [None]},
         {"name": "sda2", "path": "/dev/sda2", "type": "part", "fstype": "swap",
          "uuid": "swap-uuid-2", "label": "", "size": 1000, "mountpoints": [None]}]},
    {"name": "sdb", "path": "/dev/sdb", "type": "disk", "rm": False, "model": "",
     "size": 500107862016, "fstype": "ext4", "uuid": "1111-2222-3333", "label": "data",
     "mountpoints": ["/srv/quota-share/data"]},
]}


class FakeRun:
    def __init__(self, lsblk=LSBLK):
        self.calls: list[list[str]] = []
        self.lsblk = lsblk

    def __call__(self, argv, timeout=60):
        self.calls.append(argv)
        if argv[0] == "lsblk":
            return 0, json.dumps(self.lsblk)
        if argv[:2] == ["id", "-u"] or argv[:2] == ["id", "-g"]:
            return 0, "998"
        return 0, ""


def _mgr(tmp_path, run=None, samba=True):
    main = tmp_path / "smb.conf"
    main.write_text("[global]\n   map to guest = bad user\n\n[print$]\n   path = /var/lib/samba\n")
    return ds.DiskShareManager(run_command=run or FakeRun(), root=str(tmp_path / "share"),
                               smb_shares=str(tmp_path / "quota-shares.conf"),
                               smb_main=str(main),
                               client_dir=str(tmp_path / "clients"),
                               which=lambda b: "/usr/sbin/smbd" if samba else None)


def test_lsblk_skips_disks_mounted_elsewhere():
    """A data disk the system mounts on /opt (the app itself lives there on
    some boxes) or one auto-mounted on /media is in use: never offered."""
    data = json.loads(json.dumps(LSBLK))
    data["blockdevices"][2]["mountpoints"] = ["/opt"]
    assert {d["uuid"] for d in ds.disks_from_lsblk(data)} == {"7A3C55D23C558B6F"}


def test_lsblk_never_offers_the_system_disk():
    disks = ds.disks_from_lsblk(LSBLK)
    uuids = {d["uuid"] for d in disks}
    assert uuids == {"7A3C55D23C558B6F", "1111-2222-3333"}   # no vda*, no swap
    movies = next(d for d in disks if d["label"] == "Movies")
    assert movies["fstype"] == "ntfs" and movies["mounted"] == "" and movies["removable"]
    assert movies["model"] == "WD Elements"


def test_parse_shares_drops_bad_entries():
    raw = json.dumps([{"name": "Movies", "uuid": "7A3C55D23C558B6F"},
                      {"name": "../etc", "uuid": "x1234"},
                      {"name": "movies", "uuid": "1111-2222-3333"},  # duplicate name
                      {"name": "ok", "uuid": "bad uuid!"}, "junk"])
    assert ds.parse_shares(raw) == [{"name": "Movies", "source": "disk",
                                     "uuid": "7A3C55D23C558B6F", "writable": True,
                                     "access": "all", "devices": {}}]
    assert ds.parse_shares("not json") == []


def test_render_smb():
    text = ds.render_smb([{"name": "Movies", "uuid": "u", "writable": False}], "/srv/x")
    assert "[Movies]" in text and "path = /srv/x/Movies" in text
    assert "guest ok = yes" in text and "read only = yes" in text


def test_apply_mounts_writes_samba_and_includes_it_first(tmp_path):
    run = FakeRun()
    mgr = _mgr(tmp_path, run)
    problems = mgr.apply([{"name": "Movies", "uuid": "7A3C55D23C558B6F", "writable": True}])
    assert problems == []
    mount = next(c for c in run.calls if c[0] == "mount")
    assert mount == ["mount", "-o", "uid=998,gid=998,umask=002", "-U", "7A3C55D23C558B6F",
                     str(tmp_path / "share" / "Movies")]
    assert "[Movies]" in (tmp_path / "quota-shares.conf").read_text()
    main = (tmp_path / "smb.conf").read_text()
    # first line: our [global] options must cover every share defined after it
    assert main.splitlines()[1] == f"include = {tmp_path / 'quota-shares.conf'}"
    assert ["systemctl", "enable", "--now", "smbd"] in run.calls
    mgr.apply([{"name": "Movies", "uuid": "7A3C55D23C558B6F", "writable": True}])
    assert (tmp_path / "smb.conf").read_text().count("include =") == 1  # idempotent


def test_include_left_at_the_end_by_an_older_version_moves_to_the_top(tmp_path):
    mgr = _mgr(tmp_path)
    inc = f"include = {tmp_path / 'quota-shares.conf'}"
    main = tmp_path / "smb.conf"
    main.write_text(main.read_text() + "\n# Quota Manager disk shares\n" + inc + "\n")
    mgr.apply([])
    text = main.read_text()
    assert text.startswith(ds.INCLUDE_COMMENT + "\n" + inc + "\n")
    assert text.count("include =") == 1 and "[print$]" in text

def test_apply_read_only_mounts_ro(tmp_path):
    run = FakeRun()
    _mgr(tmp_path, run).apply([{"name": "Movies", "uuid": "7A3C55D23C558B6F", "writable": False}])
    mount = next(c for c in run.calls if c[0] == "mount")
    assert mount[2] == "uid=998,gid=998,umask=002,ro"


def test_apply_reports_missing_and_foreign_mounts(tmp_path):
    lsblk = json.loads(json.dumps(LSBLK))
    lsblk["blockdevices"][2]["mountpoints"] = ["/media/usb"]
    run = FakeRun(lsblk)
    problems = _mgr(tmp_path, run).apply([
        {"name": "Gone", "uuid": "0000-0000", "writable": True},
        {"name": "data", "uuid": "1111-2222-3333", "writable": True}])
    assert len([p for p in problems if "not connected" in p]) == 2  # in use elsewhere = not offered
    assert not any(c[0] == "mount" for c in run.calls)


def test_apply_already_mounted_at_our_folder_is_not_remounted(tmp_path):
    run = FakeRun()
    mgr = _mgr(tmp_path, run)
    mgr.root = "/srv/quota-share"
    assert mgr.apply([{"name": "data", "uuid": "1111-2222-3333", "writable": True}]) == []
    assert not any(c[0] == "mount" for c in run.calls)


def test_apply_without_samba_still_mounts_but_writes_nothing(tmp_path):
    run = FakeRun()
    _mgr(tmp_path, run, samba=False).apply(
        [{"name": "Movies", "uuid": "7A3C55D23C558B6F", "writable": True}])
    assert any(c[0] == "mount" for c in run.calls)
    assert not (tmp_path / "quota-shares.conf").exists()


def _client(tmp_path, mgr):
    database = _db.Database(tmp_path / "disks.db")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    holder.swap(EngineSnapshot())
    asyncio.new_event_loop().run_until_complete(database.connect())
    return create_app(database, service, holder, disk_share_manager=mgr), database


def test_api_share_and_unshare(tmp_path):
    lsblk = json.loads(json.dumps(LSBLK))
    lsblk["blockdevices"][2]["mountpoints"] = [None]     # second data disk, unmounted
    run = FakeRun(lsblk)
    app, database = _client(tmp_path, _mgr(tmp_path, run))
    with TestClient(app) as c:
        assert c.get("/api/disks").status_code == 401
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        d = c.get("/api/disks").json()
        assert d["samba"] is True and len(d["disks"]) == 2 and d["shares"] == []
        r = c.post("/api/disks/share", json={"uuid": "7A3C55D23C558B6F", "name": "Movies"})
        assert r.status_code == 200, r.text
        assert [s["name"] for s in r.json()["shares"]] == ["Movies"]
        assert c.post("/api/disks/share", json={"uuid": "1111-2222-3333",
                                                "name": "movies"}).status_code == 409
        assert c.post("/api/disks/share", json={"uuid": "7A3C55D23C558B6F",
                                                "name": "Other"}).status_code == 409
        assert c.post("/api/disks/share", json={"uuid": "root-uuid-1",
                                                "name": "root"}).status_code == 404
        assert c.post("/api/disks/share", json={"uuid": "7A3C55D23C558B6F",
                                                "name": "../x"}).status_code == 422
        stored = asyncio.new_event_loop().run_until_complete(
            database.get_setting("disk_shares"))
        assert json.loads(stored)[0]["name"] == "Movies"
        assert c.delete("/api/disks/share/Movies").json()["shares"] == []
        assert c.delete("/api/disks/share/Movies").status_code == 404


# ---------------------------------------------------------------------------
# network folders: a folder another PC or the router already shares
# ---------------------------------------------------------------------------

def test_valid_remote():
    assert ds.valid_remote("//192.168.1.5/Movies")
    assert ds.valid_remote("//nas.local/My Movies/2024")
    for bad in ("//h/x,uid=0", "\\\\h\\x", "//h/", "//h/x\"y", "//h/x\ny", "http://h/x"):
        assert not ds.valid_remote(bad), bad


def test_parse_shares_network_entries():
    raw = json.dumps([
        {"name": "PC", "source": "network", "remote": "//192.168.1.5/D", "user": "amr"},
        {"name": "Bad", "source": "network", "remote": "//h/x,uid=0"},
        {"name": "Bad2", "source": "network", "remote": "//h/x", "user": "a b"}])
    assert ds.parse_shares(raw) == [{"name": "PC", "source": "network",
                                     "remote": "//192.168.1.5/D", "user": "amr",
                                     "writable": True, "access": "all", "devices": {}}]


def test_credentials_refuse_line_breaks(tmp_path):
    mgr = _mgr(tmp_path, FakeRun())
    mgr.cred_dir = str(tmp_path / "creds")
    for user, pw in (("amr", "x\ndomain=evil"), ("amr\r", "x"), ("amr", "x\x00")):
        with pytest.raises(ValueError):
            mgr.save_credentials("PC", user, pw)
    assert not (tmp_path / "creds" / "PC").exists()


def test_network_folder_is_mounted_with_cifs(tmp_path):
    run = FakeRun()
    mgr = _mgr(tmp_path, run)
    mgr.cred_dir = str(tmp_path / "creds")
    mgr._ismount = lambda p: False
    mgr.save_credentials("PC", "amr", "p,ss=word")
    cred = tmp_path / "creds" / "PC"
    assert cred.read_text() == "username=amr\npassword=p,ss=word\n"
    assert oct(cred.stat().st_mode & 0o777) == "0o600"
    problems = mgr.apply([
        {"name": "PC", "source": "network", "remote": "//192.168.1.5/D", "user": "amr", "writable": True},
        {"name": "Router", "source": "network", "remote": "//192.168.1.1/usb", "user": "", "writable": False}])
    assert problems == []
    mounts = [c for c in run.calls if "mount" in c[:3] and "cifs" in c]
    assert mounts[0][:7] == ["timeout", "20", "mount", "-t", "cifs", "//192.168.1.5/D",
                             str(tmp_path / "share" / "PC")]
    assert f"credentials={cred}" in mounts[0][-1]
    assert mounts[1][-1].endswith("guest,ro")
    conf = (tmp_path / "quota-shares.conf").read_text()
    assert "[PC]" in conf and "[Router]" in conf
    mgr.forget_credentials("PC")
    assert not cred.exists()


def test_unreachable_network_folder_is_reported_and_not_published(tmp_path):
    class Fail(FakeRun):
        def __call__(self, argv, timeout=60):
            if "cifs" in argv:
                self.calls.append(argv)
                return 32, "mount error(113): could not connect"
            return super().__call__(argv, timeout)
    mgr = _mgr(tmp_path, Fail())
    mgr._ismount = lambda p: False
    problems = mgr.apply([{"name": "PC", "source": "network", "remote": "//10.0.0.9/D",
                           "user": "", "writable": True}])
    assert problems and "could not reach //10.0.0.9/D" in problems[0]
    assert "[PC]" not in (tmp_path / "quota-shares.conf").read_text()


def test_api_network_folder(tmp_path):
    run = FakeRun()
    mgr = _mgr(tmp_path, run)
    mgr.cred_dir = str(tmp_path / "creds")
    mgr._ismount = lambda p: False
    app, database = _client(tmp_path, mgr)
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        r = c.post("/api/disks/share", json={"source": "network", "name": "PC",
                                             "remote": "\\\\192.168.1.5\\D", "user": "amr",
                                             "password": "secret"})
        assert r.status_code == 200, r.text
        assert r.json()["shares"][0]["remote"] == "//192.168.1.5/D"   # backslashes accepted
        stored = asyncio.new_event_loop().run_until_complete(database.get_setting("disk_shares"))
        assert "secret" not in stored                                    # never in the DB
        assert (tmp_path / "creds" / "PC").exists()
        assert c.post("/api/disks/share", json={"source": "network", "name": "PC2",
                                                "remote": "//192.168.1.5/D"}).status_code == 409
        assert c.post("/api/disks/share", json={"source": "network", "name": "X",
                                                "remote": "//h/x,uid=0"}).status_code == 422
        assert c.post("/api/disks/share", json={"name": "Y"}).status_code == 422  # disk w/o uuid
        assert c.delete("/api/disks/share/PC").status_code == 200
        assert not (tmp_path / "creds" / "PC").exists()


# ---------------------------------------------------------------------------
# PR #44 review: LAN only, who gets in, no root, no symlinks
# ---------------------------------------------------------------------------

SHARE = {"name": "Movies", "source": "disk", "uuid": "u", "writable": True,
         "access": "all", "devices": {}}
NETS = ["192.168.2.0/24", "192.168.1.0/24", "fd00::/8"]


def _section(text, name):
    out, on = [], False
    for line in text.splitlines():
        if line.startswith("["):
            on = line == f"[{name}]"
            continue
        if on and line.strip():
            out.append(line.strip())
    return out


def test_samba_listens_on_the_lan_interface_only():
    text = ds.render_smb([SHARE], "/srv/x", "lan0", NETS)
    g = _section(text, "global")
    assert "interfaces = lo lan0" in g
    assert "bind interfaces only = yes" in g
    assert "hosts allow = 127.0.0.1 192.168.2.0/24 192.168.1.0/24" in g   # IPv6 net dropped
    assert "hosts deny = ALL" in g
    assert "disable netbios = yes" in g and "smb ports = 445" in g
    assert "ppp0" not in text


def test_unknown_lan_interface_listens_on_loopback_only():
    g = _section(ds.render_smb([SHARE], "/srv/x", "", NETS), "global")
    assert "interfaces = lo" in g


def test_a_bad_interface_name_is_never_written():
    mgr = ds.DiskShareManager(lan_if="lan0\nhosts allow = ALL")
    assert mgr.lan_if == ""


def test_shares_never_run_as_root_or_follow_symlinks():
    sec = _section(ds.render_smb([SHARE], "/srv/x", "lan0", NETS), "Movies")
    assert "force user = quotashare" in sec
    assert not any("root" in line for line in sec)
    assert "follow symlinks = no" in sec and "wide links = no" in sec


def test_cut_off_devices_are_excluded_everywhere():
    text = ds.render_smb([SHARE], "/srv/x", "lan0", NETS,
                         denied=["192.168.2.108", "192.168.2.105", "fd00::5", "junk"])
    g = _section(text, "global")
    assert ("hosts allow = 127.0.0.1 192.168.2.0/24 192.168.1.0/24 "
            "EXCEPT 192.168.2.105 192.168.2.108") in g


def test_selected_devices_only():
    share = dict(SHARE, access="selected",
                 devices={"aa:aa:aa:aa:aa:01": "rw", "aa:aa:aa:aa:aa:02": "ro",
                          "aa:aa:aa:aa:aa:03": "none"})
    ips = {"aa:aa:aa:aa:aa:01": "192.168.2.101", "aa:aa:aa:aa:aa:02": "192.168.2.102",
           "aa:aa:aa:aa:aa:03": "192.168.2.103", "aa:aa:aa:aa:aa:04": "192.168.2.104"}
    sec = _section(ds.render_smb([share], "/srv/x", "lan0", NETS, ip_by_mac=ips,
                                 client_dir="/c"), "Movies")
    assert "read only = yes" in sec
    assert "hosts allow = 127.0.0.1 192.168.2.101 192.168.2.102" in sec
    assert "include = /c/Movies-%I.conf" in sec
    files = ds.render_client_overrides([share], ips)
    assert files == {"Movies-192.168.2.101.conf": "   read only = no\n"}


def test_selected_device_that_is_cut_off_stays_out():
    share = dict(SHARE, access="selected", devices={"aa:aa:aa:aa:aa:01": "rw"})
    ips = {"aa:aa:aa:aa:aa:01": "192.168.2.101"}
    sec = _section(ds.render_smb([share], "/srv/x", "lan0", NETS, ["192.168.2.101"], ip_by_mac=ips),
                   "Movies")
    assert "hosts allow = 127.0.0.1" in sec
    assert ds.render_client_overrides([share], ips, ["192.168.2.101"]) == {}


def test_all_devices_with_per_device_overrides():
    share = dict(SHARE, writable=False,
                 devices={"aa:aa:aa:aa:aa:01": "rw", "aa:aa:aa:aa:aa:03": "none"})
    ips = {"aa:aa:aa:aa:aa:01": "192.168.2.101", "aa:aa:aa:aa:aa:02": "192.168.2.102",
           "aa:aa:aa:aa:aa:03": "192.168.2.103"}
    sec = _section(ds.render_smb([share], "/srv/x", "lan0", NETS, ["192.168.2.108"],
                                 ip_by_mac=ips), "Movies")
    assert "read only = yes" in sec
    assert ("hosts allow = 127.0.0.1 192.168.2.0/24 192.168.1.0/24 "
            "EXCEPT 192.168.2.103 192.168.2.108") in sec
    assert ds.render_client_overrides([share], ips) == {
        "Movies-192.168.2.101.conf": "   read only = no\n"}


def test_mount_read_write_only_when_someone_may_write():
    assert ds.share_needs_rw(SHARE)
    assert not ds.share_needs_rw(dict(SHARE, writable=False))
    assert ds.share_needs_rw(dict(SHARE, writable=False, devices={"aa:aa:aa:aa:aa:01": "rw"}))
    assert not ds.share_needs_rw(dict(SHARE, access="selected",
                                      devices={"aa:aa:aa:aa:aa:01": "ro"}))


def test_parse_shares_cleans_access_and_devices():
    raw = json.dumps([{"name": "M", "uuid": "abcd-1234", "access": "weird",
                       "devices": {"AA:AA:AA:AA:AA:01": "rw", "bad": "rw",
                                   "aa:aa:aa:aa:aa:02": "admin"}}])
    s = ds.parse_shares(raw)[0]
    assert s["access"] == "all" and s["devices"] == {"aa:aa:aa:aa:aa:01": "rw"}


def test_set_clients_is_cache_gated_and_reloads_samba(tmp_path):
    run = FakeRun()
    mgr = _mgr(tmp_path, run)
    mgr.client_dir = str(tmp_path / "clients")
    share = dict(SHARE, uuid="7A3C55D23C558B6F", writable=False,
                 devices={"aa:aa:aa:aa:aa:01": "rw"})
    mgr.apply([share])
    run.calls.clear()
    assert mgr.set_clients({"192.168.2.108"}, {"aa:aa:aa:aa:aa:01": "192.168.2.101"}) is True
    assert ["smbcontrol", "smbd", "reload-config"] in run.calls
    conf = (tmp_path / "quota-shares.conf").read_text()
    assert "EXCEPT 192.168.2.108" in conf
    assert (tmp_path / "clients" / "Movies-192.168.2.101.conf").read_text() == "   read only = no\n"
    run.calls.clear()
    assert mgr.set_clients({"192.168.2.108"}, {"aa:aa:aa:aa:aa:01": "192.168.2.101"}) is False
    assert run.calls == []                                    # nothing changed, nothing done
    mgr.set_clients(set(), {"aa:aa:aa:aa:aa:01": "192.168.2.150"})   # device moved IP
    assert not (tmp_path / "clients" / "Movies-192.168.2.101.conf").exists()
    assert (tmp_path / "clients" / "Movies-192.168.2.150.conf").exists()


def test_changing_the_level_remounts_the_share(tmp_path):
    lsblk = json.loads(json.dumps(LSBLK))
    run = FakeRun(lsblk)
    mgr = _mgr(tmp_path, run)
    share = dict(SHARE, uuid="7A3C55D23C558B6F", writable=False)
    mgr.apply([share])
    first = [c for c in run.calls if c[0] == "mount"]
    assert first[-1][2].endswith(",ro")
    # now mounted at our folder; the admin makes it writable
    lsblk["blockdevices"][1]["children"][0]["mountpoints"] = [str(tmp_path / "share" / "Movies")]
    run.calls.clear()
    mgr.apply([dict(share, writable=True)])
    assert ["umount", "-l", str(tmp_path / "share" / "Movies")] in run.calls
    remount = [c for c in run.calls if c[0] == "mount"][-1]
    assert not remount[2].endswith(",ro")


# ---------------------------------------------------------------------------
# unplug / replug, unreachable network folders
# ---------------------------------------------------------------------------

def test_heal_unmounts_an_unplugged_disk_and_remounts_it_when_back(tmp_path):
    lsblk = json.loads(json.dumps(LSBLK))
    run = FakeRun(lsblk)
    mounted = set()
    mgr = _mgr(tmp_path, run)
    mgr._ismount = lambda p: p in mounted
    share = dict(SHARE, uuid="7A3C55D23C558B6F")
    target = str(tmp_path / "share" / "Movies")
    mgr.apply([share])
    mounted.add(target)
    lsblk["blockdevices"][1]["children"][0]["mountpoints"] = [target]
    assert mgr.heal([share]) is False                         # healthy: nothing to do
    # unplugged
    lsblk["blockdevices"].pop(1)
    run.calls.clear()
    assert mgr.heal([share]) is True
    assert ["umount", "-l", target] in run.calls
    assert "[Movies]" not in (tmp_path / "quota-shares.conf").read_text()
    mounted.discard(target)
    assert mgr.heal([share]) is False                         # still gone: quiet
    # plugged back in
    lsblk["blockdevices"].insert(1, json.loads(json.dumps(LSBLK))["blockdevices"][1])
    run.calls.clear()
    assert mgr.heal([share]) is True
    assert any(c[0] == "mount" for c in run.calls)
    assert "[Movies]" in (tmp_path / "quota-shares.conf").read_text()


def test_unreachable_network_folder_backs_off_and_is_bounded(tmp_path):
    now = [1000.0]
    calls = []

    class Down(FakeRun):
        def __call__(self, argv, timeout=60):
            if "cifs" in argv:
                calls.append((argv, timeout))
                return 124, "timed out after 25 s"
            return super().__call__(argv, timeout)
    mgr = _mgr(tmp_path, Down())
    mgr._ismount = lambda p: False
    mgr._clock = lambda: now[0]
    share = {"name": "PC", "source": "network", "remote": "//10.0.0.9/D", "user": "",
             "writable": True, "access": "all", "devices": {}}
    mgr.apply([share])
    argv, timeout = calls[-1]
    assert argv[:2] == ["timeout", str(ds.CIFS_MOUNT_TIMEOUT_S)] and timeout <= 30
    assert mgr.heal([share]) is False and len(calls) == 1     # inside the back-off
    now[0] += ds.NETWORK_RETRY_S
    mgr.heal([share])
    assert len(calls) == 2                                    # retried after it


def test_startup_does_not_wait_for_shares(tmp_path):
    """An unreachable network folder must not hold up startup: the shares are
    restored in the background once the app is serving."""
    import threading
    import time as _time

    gate = threading.Event()

    class Slow(ds.DiskShareManager):
        def apply(self, shares, force_network=True):
            gate.wait(5)
            return []
    mgr = Slow(root=str(tmp_path / "share"), smb_shares=str(tmp_path / "q.conf"),
               which=lambda b: None)
    app, database = _client(tmp_path, mgr)
    asyncio.new_event_loop().run_until_complete(database.set_setting(
        "disk_shares", json.dumps([{"name": "PC", "source": "network",
                                    "remote": "//10.0.0.9/D"}])))
    t0 = _time.monotonic()
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        assert _time.monotonic() - t0 < 3
        gate.set()


def test_api_change_access(tmp_path):
    run = FakeRun()
    app, _ = _client(tmp_path, _mgr(tmp_path, run))
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": "admin"}).status_code == 200
        assert c.post("/api/disks/share", json={"uuid": "7A3C55D23C558B6F",
                                                "name": "Movies"}).status_code == 200
        r = c.put("/api/disks/share/Movies", json={
            "access": "selected", "writable": False,
            "devices": {"AA:AA:AA:AA:AA:01": "rw", "nope": "rw"}})
        assert r.status_code == 200, r.text
        s = r.json()["shares"][0]
        assert s["access"] == "selected" and s["devices"] == {"aa:aa:aa:aa:aa:01": "rw"}
        assert c.put("/api/disks/share/Nope", json={}).status_code == 404
        assert c.put("/api/disks/share/Movies", json={"access": "everyone"}).status_code == 422


def test_first_share_restarts_samba_so_the_lan_binding_takes_effect(tmp_path):
    """Found on a real box: smbd kept listening on 0.0.0.0:445 after a reload,
    because `interfaces`/`bind interfaces only` are read only at start."""
    run = FakeRun()
    mgr = _mgr(tmp_path, run)
    mgr.lan_if = "lan0"
    share = dict(SHARE, uuid="7A3C55D23C558B6F")
    mgr.apply([share])
    assert ["systemctl", "restart", "smbd"] in run.calls
    run.calls.clear()
    mgr.set_clients({"192.168.2.108"}, {})          # only hosts allow changes
    assert ["systemctl", "restart", "smbd"] not in run.calls
    assert ["smbcontrol", "smbd", "reload-config"] in run.calls
    run.calls.clear()
    mgr.lan_if = "eth1"                             # binding changes again
    mgr.apply([share])
    assert ["systemctl", "restart", "smbd"] in run.calls


def test_first_write_after_start_restarts_samba_even_if_unchanged(tmp_path):
    """Found on a real reboot: smbd started before the gateway added
    192.168.2.1 and listened on the uplink address only; the config had not
    changed, so a reload never re-bound it."""
    run = FakeRun()
    mgr = _mgr(tmp_path, run)
    mgr.lan_if = "lan0"
    share = dict(SHARE, uuid="7A3C55D23C558B6F")
    mgr.apply([share])                       # writes the file
    fresh = _mgr(tmp_path, FakeRun())        # app restarted, same config on disk
    fresh.lan_if = "lan0"
    fresh.apply([share])
    assert ["systemctl", "restart", "smbd"] in fresh._run.calls
    fresh._run.calls.clear()
    fresh.apply([share])                     # later writes only reload
    assert ["systemctl", "restart", "smbd"] not in fresh._run.calls
