"""Share > Network drives: disks plugged into the gateway shared over SMB."""
from __future__ import annotations

import asyncio
import json

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

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "lsblk":
            return 0, json.dumps(self.lsblk)
        return 0, ""


def _mgr(tmp_path, run=None, samba=True):
    main = tmp_path / "smb.conf"
    main.write_text("[global]\n   map to guest = bad user\n\n[print$]\n   path = /var/lib/samba\n")
    return ds.DiskShareManager(run_command=run or FakeRun(), root=str(tmp_path / "share"),
                               smb_shares=str(tmp_path / "quota-shares.conf"),
                               smb_main=str(main),
                               which=lambda b: "/usr/sbin/smbd" if samba else None)


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
                                     "uuid": "7A3C55D23C558B6F", "writable": True}]
    assert ds.parse_shares("not json") == []


def test_render_smb():
    text = ds.render_smb([{"name": "Movies", "uuid": "u", "writable": False}], "/srv/x")
    assert "[Movies]" in text and "path = /srv/x/Movies" in text
    assert "guest ok = yes" in text and "read only = yes" in text


def test_apply_mounts_writes_samba_and_includes_it(tmp_path):
    run = FakeRun()
    mgr = _mgr(tmp_path, run)
    problems = mgr.apply([{"name": "Movies", "uuid": "7A3C55D23C558B6F", "writable": True}])
    assert problems == []
    mount = next(c for c in run.calls if c[0] == "mount")
    assert mount == ["mount", "-o", "uid=0,gid=0,umask=000", "-U", "7A3C55D23C558B6F",
                     str(tmp_path / "share" / "Movies")]
    assert "[Movies]" in (tmp_path / "quota-shares.conf").read_text()
    main = (tmp_path / "smb.conf").read_text()
    assert main.count(ds.INCLUDE_LINE.replace(ds.SMB_SHARES, "")) >= 1
    assert ["systemctl", "enable", "--now", "smbd"] in run.calls
    mgr.apply([{"name": "Movies", "uuid": "7A3C55D23C558B6F", "writable": True}])
    assert (tmp_path / "smb.conf").read_text().count("include =") == 1  # idempotent


def test_apply_read_only_mounts_ro(tmp_path):
    run = FakeRun()
    _mgr(tmp_path, run).apply([{"name": "Movies", "uuid": "7A3C55D23C558B6F", "writable": False}])
    mount = next(c for c in run.calls if c[0] == "mount")
    assert mount[2] == "uid=0,gid=0,umask=000,ro"


def test_apply_reports_missing_and_foreign_mounts(tmp_path):
    lsblk = json.loads(json.dumps(LSBLK))
    lsblk["blockdevices"][2]["mountpoints"] = ["/media/usb"]
    run = FakeRun(lsblk)
    problems = _mgr(tmp_path, run).apply([
        {"name": "Gone", "uuid": "0000-0000", "writable": True},
        {"name": "data", "uuid": "1111-2222-3333", "writable": True}])
    assert any("not connected" in p for p in problems)
    assert any("already mounted at /media/usb" in p for p in problems)
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
    run = FakeRun()
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
                                     "writable": True}]


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
    mounts = [c for c in run.calls if c[0] == "mount"]
    assert mounts[0][:5] == ["mount", "-t", "cifs", "//192.168.1.5/D", str(tmp_path / "share" / "PC")]
    assert f"credentials={cred}" in mounts[0][-1]
    assert mounts[1][-1].endswith("guest,ro")
    conf = (tmp_path / "quota-shares.conf").read_text()
    assert "[PC]" in conf and "[Router]" in conf
    mgr.forget_credentials("PC")
    assert not cred.exists()


def test_unreachable_network_folder_is_reported_and_not_published(tmp_path):
    class Fail(FakeRun):
        def __call__(self, argv):
            self.calls.append(argv)
            if argv[0] == "mount":
                return 32, "mount error(113): could not connect"
            return super().__call__(argv) if argv[0] == "lsblk" else (0, "")
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
