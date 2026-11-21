"""Share hard disks plugged into the gateway with the whole home network (SMB).

Two kinds of source, both mounted under ``/srv/quota-share/<name>`` and
published as a Samba share that every phone, PC and TV on the network can
open (``\\\\192.168.2.1\\<name>`` or ``smb://192.168.2.1/<name>``), with no
password, read-write or read-only:

- ``disk``: a hard disk plugged into the gateway itself (picked by UUID);
- ``network``: a folder already shared by a PC or router on the network
  (``//192.168.1.5/Movies``), mounted with CIFS. Handy when the disk sits in
  another computer, or in the router's USB port on the other subnet: the
  gateway gives every device one address for it.

The list of shares lives in the DB (``settings.disk_shares``) and is applied
again at every start (golden rule 8), so a reboot or an update brings the
shares back on their own. Everything host-touching goes through an injectable
``run`` so the tests never mount anything.

Never touches the disk the system runs from, and only mounts filesystems it
knows (ext4/ext3/ext2, btrfs, xfs, ntfs, exfat, vfat).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("quota.disk_share")

RunCommand = Callable[[list[str]], tuple[int, str]]

SETTING_KEY = "disk_shares"
SHARE_ROOT = "/srv/quota-share"
SMB_SHARES = "/etc/samba/quota-shares.conf"
SMB_MAIN = "/etc/samba/smb.conf"
CRED_DIR = "/etc/quota-gateway/smb-credentials"
INCLUDE_LINE = f"include = {SMB_SHARES}"

#: filesystems we mount, with the options that make them writable by the share.
FS_OPTIONS: dict[str, str] = {
    "ext4": "", "ext3": "", "ext2": "", "btrfs": "", "xfs": "",
    "ntfs": "uid=0,gid=0,umask=000", "ntfs3": "uid=0,gid=0,umask=000",
    "exfat": "uid=0,gid=0,umask=000", "vfat": "uid=0,gid=0,umask=000",
}
#: mount points that mean "this disk runs the system — never offer it".
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/usr", "/var", "/home", "[SWAP]"}

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_UUID_RE = re.compile(r"^[A-Za-z0-9-]{4,64}$")
#: //host/share[/sub/dir] — no backslashes, quotes, commas or control characters
#: (it ends up on a mount command line and in mount options).
_REMOTE_RE = re.compile(r"^//[A-Za-z0-9.-]{1,253}/[^/\\\x00-\x1f,'\"]{1,80}(/[^\\\x00-\x1f,'\"]{1,200})?$")
_USER_RE = re.compile(r"^[A-Za-z0-9._@\\-]{1,64}$")


def valid_remote(remote: str) -> bool:
    return bool(_REMOTE_RE.match(remote or ""))


def _default_run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return 127, f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def valid_name(name: str) -> bool:
    """Share names become a folder and a Samba section: keep them plain."""
    return bool(_NAME_RE.match(name or ""))


def parse_shares(raw: str | None) -> list[dict[str, Any]]:
    """The stored share list; anything malformed is dropped, never trusted."""
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return []
    out, seen = [], set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", ""))
        if not valid_name(name) or name.lower() in seen:
            continue
        writable = bool(it.get("writable", True))
        if it.get("source", "disk") == "network":
            remote, user = str(it.get("remote", "")), str(it.get("user", ""))
            if not valid_remote(remote) or (user and not _USER_RE.match(user)):
                continue
            entry = {"name": name, "source": "network", "remote": remote,
                     "user": user, "writable": writable}
        else:
            uuid = str(it.get("uuid", ""))
            if not _UUID_RE.match(uuid):
                continue
            entry = {"name": name, "source": "disk", "uuid": uuid, "writable": writable}
        seen.add(name.lower())
        out.append(entry)
    return out


def _flatten(devices: list[dict], parent: dict | None = None) -> list[tuple[dict, dict | None]]:
    out = []
    for dev in devices or []:
        out.append((dev, parent))
        out.extend(_flatten(dev.get("children") or [], parent or dev))
    return out


def disks_from_lsblk(data: dict) -> list[dict[str, Any]]:
    """Shareable filesystems from ``lsblk -J`` output.

    A whole disk is skipped (with every partition on it) when any of its
    filesystems is mounted at a system path, so the system disk is never
    offered.
    """
    tops = data.get("blockdevices") or []
    system_disks = set()
    for dev, parent in _flatten(tops):
        top = parent or dev
        mounts = set(dev.get("mountpoints") or []) | {dev.get("mountpoint")}
        if mounts & SYSTEM_MOUNTS:
            system_disks.add(top.get("name"))
    out = []
    for dev, parent in _flatten(tops):
        top = parent or dev
        fstype = (dev.get("fstype") or "").lower()
        if top.get("name") in system_disks or fstype not in FS_OPTIONS or not dev.get("uuid"):
            continue
        if dev.get("type") not in ("part", "disk"):
            continue
        mounts = [m for m in (dev.get("mountpoints") or [dev.get("mountpoint")]) if m]
        out.append({
            "uuid": dev["uuid"], "path": dev.get("path") or f"/dev/{dev.get('name')}",
            "label": dev.get("label") or "", "fstype": fstype,
            "size": int(dev.get("size") or 0), "mounted": mounts[0] if mounts else "",
            "model": (top.get("model") or "").strip(), "removable": bool(top.get("rm")),
        })
    return out


def render_smb(shares: list[dict[str, Any]], root: str = SHARE_ROOT) -> str:
    lines = ["# Quota Manager — generated, do not edit by hand.",
             "# Disks shared from the dashboard (Share > Network drives)."]
    for s in shares:
        lines += [
            "", f"[{s['name']}]",
            f"   path = {root}/{s['name']}",
            "   browseable = yes",
            "   guest ok = yes",
            f"   read only = {'no' if s['writable'] else 'yes'}",
            "   force user = root",
            "   create mask = 0666",
            "   directory mask = 0777",
        ]
    return "\n".join(lines) + "\n"


class DiskShareManager:
    def __init__(self, run_command: RunCommand | None = None, root: str = SHARE_ROOT,
                 smb_shares: str = SMB_SHARES, smb_main: str = SMB_MAIN,
                 which: Callable[[str], str | None] | None = None,
                 cred_dir: str = CRED_DIR,
                 ismount: Callable[[str], bool] = os.path.ismount) -> None:
        self._run = run_command or _default_run
        self.root = root
        self.smb_shares = smb_shares
        self.smb_main = smb_main
        # /usr/sbin is not always on the service's PATH.
        self._which = which or (lambda b: shutil.which(
            b, path=os.environ.get("PATH", "") + ":/usr/sbin:/sbin"))
        self.cred_dir = cred_dir
        self._ismount = ismount
        self._lock = threading.Lock()
        self._installing = False
        self._install_error = ""

    # ---------------------------------------------------------------- state
    def samba_installed(self) -> bool:
        return bool(self._which("smbd"))

    def has_state(self) -> bool:
        """True when earlier shares may have left mounts or a Samba file."""
        return Path(self.smb_shares).exists() or Path(self.root).is_dir()

    def cifs_installed(self) -> bool:
        return bool(self._which("mount.cifs"))

    def list_disks(self) -> list[dict[str, Any]]:
        rc, out = self._run(["lsblk", "-J", "-b", "-o",
                             "NAME,PATH,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,TYPE,RM,MODEL"])
        if rc != 0:
            log.warning("lsblk failed: %s", out)
            return []
        try:
            return disks_from_lsblk(json.loads(out))
        except ValueError:
            return []

    def status(self, shares: list[dict[str, Any]]) -> dict[str, Any]:
        disks = self.list_disks()
        by_uuid = {d["uuid"]: d for d in disks}
        view = []
        for s in shares:
            target = f"{self.root}/{s['name']}"
            if s.get("source") == "network":
                active = self._ismount(target)
                view.append({**s, "present": active, "active": active, "disk": None})
                continue
            d = by_uuid.get(s["uuid"])
            view.append({**s, "present": d is not None,
                         "active": bool(d and d["mounted"] == target),
                         "disk": d})
        return {"samba": self.samba_installed(), "cifs": self.cifs_installed(),
                "installing": self._installing,
                "install_error": self._install_error, "disks": disks, "shares": view}

    # ---------------------------------------------------------------- apply
    def apply(self, shares: list[dict[str, Any]]) -> list[str]:
        """Mount every share, write the Samba config and (re)load Samba.
        Unmounts our folders that are no longer shared. Returns problems."""
        with self._lock:
            problems: list[str] = []
            wanted = {s["name"] for s in shares}
            disks = {d["uuid"]: d for d in self.list_disks()}
            # unmount what was unshared
            root = Path(self.root)
            if root.is_dir():
                for child in root.iterdir():
                    if child.name not in wanted and self._ismount(str(child)):
                        self._run(["umount", str(child)])
            live = []
            for s in shares:
                target = f"{self.root}/{s['name']}"
                if s.get("source") == "network":
                    problem = self._mount_network(s, target)
                    if problem:
                        problems.append(f"{s['name']}: {problem}")
                    else:
                        live.append(s)
                    continue
                d = disks.get(s["uuid"])
                if d is None:
                    problems.append(f"{s['name']}: disk not connected")
                    continue
                if d["mounted"] and d["mounted"] != target:
                    problems.append(f"{s['name']}: disk is already mounted at {d['mounted']}")
                    continue
                if not d["mounted"]:
                    os.makedirs(target, exist_ok=True)
                    opts = [o for o in (FS_OPTIONS[d["fstype"]], "" if s["writable"] else "ro") if o]
                    argv = ["mount", "-U", s["uuid"], target]
                    if opts:
                        argv[1:1] = ["-o", ",".join(opts)]
                    rc, out = self._run(argv)
                    if rc != 0:
                        problems.append(f"{s['name']}: mount failed: {out}")
                        continue
                live.append(s)
            self._write_samba(live)
            for p in problems:
                log.warning("disk share: %s", p)
            return problems

    # ------------------------------------------------------------- network
    def cred_file(self, name: str) -> str:
        return os.path.join(self.cred_dir, name)

    def save_credentials(self, name: str, user: str, password: str) -> None:
        """Keep a network folder's login in a root-only file (never in the DB)."""
        os.makedirs(self.cred_dir, mode=0o700, exist_ok=True)
        path = self.cred_file(name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"username={user}\npassword={password}\n")

    def forget_credentials(self, name: str) -> None:
        try:
            os.unlink(self.cred_file(name))
        except OSError:
            pass

    def _mount_network(self, s: dict[str, Any], target: str) -> str:
        if self._ismount(target):
            return ""
        os.makedirs(target, exist_ok=True)
        opts = ["uid=0", "gid=0", "file_mode=0666", "dir_mode=0777"]
        if s.get("user"):
            opts.append(f"credentials={self.cred_file(s['name'])}")
        else:
            opts.append("guest")
        if not s["writable"]:
            opts.append("ro")
        rc, out = self._run(["mount", "-t", "cifs", s["remote"], target, "-o", ",".join(opts)])
        if rc != 0:
            return f"could not reach {s['remote']}: {out}"
        return ""

    def _write_samba(self, shares: list[dict[str, Any]]) -> None:
        if not self.samba_installed():
            return
        try:
            Path(self.smb_shares).write_text(render_smb(shares, self.root), encoding="utf-8")
            main = Path(self.smb_main)
            text = main.read_text(encoding="utf-8") if main.exists() else "[global]\n"
            if INCLUDE_LINE not in text:
                main.write_text(text.rstrip("\n") + "\n\n# Quota Manager disk shares\n"
                                + INCLUDE_LINE + "\n", encoding="utf-8")
        except OSError as exc:
            log.error("could not write the Samba config: %s", exc)
            return
        if shares:
            self._run(["systemctl", "enable", "--now", "smbd"])
            self._run(["systemctl", "reload-or-restart", "smbd"])
        else:
            self._run(["systemctl", "reload", "smbd"])

    # -------------------------------------------------------------- install
    def install_samba(self) -> bool:
        """Install Samba in the background (Debian/Ubuntu/Kali). False when
        already installed or already installing."""
        with self._lock:
            if self._installing or (self.samba_installed() and self.cifs_installed()):
                return False
            self._installing = True
            self._install_error = ""
        threading.Thread(target=self._install_worker, name="samba-install", daemon=True).start()
        return True

    def _install_worker(self) -> None:
        try:
            rc, out = self._run(["systemd-run", "--unit=quota-samba-install", "--collect",
                                 "--quiet", "--wait", "--property=Type=oneshot",
                                 "--property=Environment=DEBIAN_FRONTEND=noninteractive",
                                 "apt-get", "install", "-y", "samba", "cifs-utils"])
            if rc != 0:
                self._install_error = (out or f"exit {rc}")[-300:]
        finally:
            self._installing = False
