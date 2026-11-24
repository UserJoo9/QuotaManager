"""Share storage with the whole home network over SMB (Share > Network drives).

Two kinds of source, both mounted under ``/srv/quota-share/<name>`` and
published as a Samba share every phone, PC and TV on the home network can open
(``\\\\192.168.2.1\\<name>`` or ``smb://192.168.2.1/<name>``), without a
password, read-write or read-only:

- ``disk``: a disk plugged into the gateway itself, picked by filesystem UUID.
  A disk the system has mounted anywhere (``/``, ``/boot``, a data disk on
  ``/opt``, swap...) is never offered.
- ``network``: a folder another PC or the router already shares
  (``//192.168.1.5/Movies``), mounted with CIFS. This also gives a disk on the
  router's USB port one address on the client subnet.

Who can reach the shares (see ``render_smb``):

- Samba listens on the LAN interface only (``interfaces`` + ``bind interfaces
  only``), never on ``ppp0``, so nothing is exposed on the ISP side in WAN mode.
- ``hosts allow`` admits the home networks only (client subnet + uplink, from
  the app's config), minus every device the gateway currently cuts off: over
  quota, paused, unassigned, or a guest still waiting for approval
  (``set_clients``, fed by the maintenance tick, cache-gated).
- Per share, the admin picks who gets in: ``access="all"`` (every device with
  internet, at the share's default ``writable`` level) or ``access="selected"``
  (only the devices listed), and can give any device its own level in
  ``devices`` ({mac: "rw" | "ro" | "none"}). Samba sees clients by IP, so the
  tick maps each MAC to its current IP: a share-level ``hosts allow`` decides
  who connects, and a per-client include (``include = .../<share>-%I.conf``)
  sets that client's ``read only``.
- Writes run as the dedicated system user ``quotashare``, not root; symlinks
  are not followed, so a share cannot reach outside its folder.
- IPv4 only: the allow list holds IPv4 networks, so clients reach the shares at
  the gateway's IPv4 address.

The share list lives in the DB (``settings.disk_shares``) and is applied again
at every start (golden rule 8). ``heal`` runs every minute: a disk that was
unplugged is unmounted, and remounted when it comes back; an unreachable
network folder is retried every few minutes with a bounded mount timeout, so it
never holds up startup or the API. Everything host-touching goes through an
injectable ``run`` so the tests never mount anything.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("quota.disk_share")

RunCommand = Callable[..., tuple[int, str]]

SETTING_KEY = "disk_shares"
SHARE_ROOT = "/srv/quota-share"
SMB_SHARES = "/etc/samba/quota-shares.conf"
SMB_MAIN = "/etc/samba/smb.conf"
SMB_CLIENT_DIR = "/etc/samba/quota-share.d"
CRED_DIR = "/etc/quota-gateway/smb-credentials"
SHARE_USER = "quotashare"
INCLUDE_COMMENT = "# Quota Manager disk shares (must stay first: it sets [global] options)"

#: seconds a CIFS mount may take before it is given up (unreachable host).
CIFS_MOUNT_TIMEOUT_S = 20
#: retry an unreachable network folder at most this often.
NETWORK_RETRY_S = 300

#: filesystems we mount. Non-POSIX ones get the share user's uid/gid at mount.
POSIX_FS = {"ext4", "ext3", "ext2", "btrfs", "xfs"}
NON_POSIX_FS = {"ntfs", "ntfs3", "exfat", "vfat"}
SUPPORTED_FS = POSIX_FS | NON_POSIX_FS
#: mount points that always mean "this disk runs the system".
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/usr", "/var", "/home", "[SWAP]"}

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_UUID_RE = re.compile(r"^[A-Za-z0-9-]{4,64}$")
_IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
#: //host/share[/sub/dir] — no backslashes, quotes, commas or control characters
#: (it ends up on a mount command line and in mount options).
_REMOTE_RE = re.compile(r"^//[A-Za-z0-9.-]{1,253}/[^/\\\x00-\x1f,'\"]{1,80}(/[^\\\x00-\x1f,'\"]{1,200})?$")
_USER_RE = re.compile(r"^[A-Za-z0-9._@\\-]{1,64}$")
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
MODES = ("rw", "ro", "none")


def clean_devices(raw: Any) -> dict[str, str]:
    """{mac: "rw"|"ro"|"none"}; anything else is dropped."""
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for mac, mode in raw.items():
            mac = str(mac).strip().lower()
            if _MAC_RE.match(mac) and mode in MODES:
                out[mac] = mode
    return out


def _default_run(argv: list[str], timeout: float = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f} s"
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def valid_name(name: str) -> bool:
    """Share names become a folder and a Samba section: keep them plain."""
    return bool(_NAME_RE.match(name or ""))


def valid_remote(remote: str) -> bool:
    return bool(_REMOTE_RE.match(remote or ""))


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
        access = it.get("access", "all") if it.get("access") in ("all", "selected") else "all"
        common = {"writable": writable, "access": access,
                  "devices": clean_devices(it.get("devices"))}
        if it.get("source", "disk") == "network":
            remote, user = str(it.get("remote", "")), str(it.get("user", ""))
            if not valid_remote(remote) or (user and not _USER_RE.match(user)):
                continue
            entry = {"name": name, "source": "network", "remote": remote,
                     "user": user, **common}
        else:
            uuid = str(it.get("uuid", ""))
            if not _UUID_RE.match(uuid):
                continue
            entry = {"name": name, "source": "disk", "uuid": uuid, **common}
        seen.add(name.lower())
        out.append(entry)
    return out


def _flatten(devices: list[dict], parent: dict | None = None) -> list[tuple[dict, dict | None]]:
    out = []
    for dev in devices or []:
        out.append((dev, parent))
        out.extend(_flatten(dev.get("children") or [], parent or dev))
    return out


def disks_from_lsblk(data: dict, root: str = SHARE_ROOT) -> list[dict[str, Any]]:
    """Shareable filesystems from ``lsblk -J`` output.

    A whole disk is skipped (with every partition on it) when any of its
    filesystems is mounted anywhere outside ``root``: that disk belongs to the
    system, so it is never offered. Unmounted filesystems and our own shares
    are listed.
    """
    tops = data.get("blockdevices") or []
    prefix = root.rstrip("/") + "/"
    system_disks = set()
    for dev, parent in _flatten(tops):
        top = parent or dev
        mounts = {m for m in (dev.get("mountpoints") or []) + [dev.get("mountpoint")] if m}
        if mounts & SYSTEM_MOUNTS or any(not m.startswith(prefix) for m in mounts):
            system_disks.add(top.get("name"))
    out = []
    for dev, parent in _flatten(tops):
        top = parent or dev
        fstype = (dev.get("fstype") or "").lower()
        if top.get("name") in system_disks or fstype not in SUPPORTED_FS or not dev.get("uuid"):
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


def ipv4_networks(nets: Iterable[str]) -> list[str]:
    """The IPv4 networks from ``nets`` in canonical form; anything else dropped."""
    out = []
    for n in nets or []:
        try:
            net = ipaddress.ip_network(str(n).strip(), strict=False)
        except ValueError:
            continue
        if net.version == 4 and str(net) not in out:
            out.append(str(net))
    return out


def _sorted_ips(ips: Iterable[str]) -> list[str]:
    return sorted({str(ipaddress.ip_address(d)) for d in ips or [] if _is_ipv4(d)},
                  key=lambda a: ipaddress.ip_address(a))


def share_mode(share: dict[str, Any], mac: str) -> str:
    """What one device may do on a share: "rw", "ro" or "none"."""
    mode = share.get("devices", {}).get(mac)
    if mode:
        return mode
    if share.get("access") == "selected":
        return "none"
    return "rw" if share.get("writable", True) else "ro"


def share_needs_rw(share: dict[str, Any]) -> bool:
    """Mount read-write when anyone may write."""
    if share.get("access", "all") == "all" and share.get("writable", True):
        return True
    return any(m == "rw" for m in share.get("devices", {}).values())


def render_client_overrides(shares: list[dict[str, Any]], ip_by_mac: dict[str, str],
                            denied: Iterable[str] = ()) -> dict[str, str]:
    """Per-client files ``<share>-<ip>.conf`` for devices whose level differs
    from the share's default ``read only``."""
    deny = set(_sorted_ips(denied))
    out: dict[str, str] = {}
    for s in shares:
        default_ro = s.get("access") == "selected" or not s.get("writable", True)
        for mac, ip in sorted(ip_by_mac.items()):
            if not _is_ipv4(ip) or ip in deny:
                continue
            mode = share_mode(s, mac)
            if mode == "none":
                continue
            if (mode == "ro") != default_ro:
                out[f"{s['name']}-{ip}.conf"] = f"   read only = {'yes' if mode == 'ro' else 'no'}\n"
    return out


def render_smb(shares: list[dict[str, Any]], root: str = SHARE_ROOT,
               lan_if: str = "", allowed_nets: Iterable[str] = (),
               denied: Iterable[str] = (), user: str = SHARE_USER,
               ip_by_mac: dict[str, str] | None = None,
               client_dir: str = SMB_CLIENT_DIR) -> str:
    """The Samba config the app owns. It is included FIRST in smb.conf, so its
    [global] options apply to every share defined after it, Debian's own
    [homes]/[printers] included."""
    ip_by_mac = ip_by_mac or {}
    nets = ipv4_networks(allowed_nets)
    deny = _sorted_ips(denied)
    allow = "127.0.0.1 " + " ".join(nets) if nets else "127.0.0.1"
    if deny:
        allow += " EXCEPT " + " ".join(deny)
    lines = ["# Quota Manager — generated, do not edit by hand.",
             "# Storage shared from the dashboard (Share > Network drives).",
             "[global]",
             # LAN only: never answer on ppp0 (WAN mode) or any other link.
             f"   interfaces = lo {lan_if}".rstrip() if lan_if else "   interfaces = lo",
             "   bind interfaces only = yes",
             # The home networks, minus every device the gateway has cut off.
             f"   hosts allow = {allow}",
             "   hosts deny = ALL",
             "   map to guest = Bad User",
             "   disable netbios = yes",
             "   smb ports = 445"]
    for s in shares:
        default_ro = s.get("access") == "selected" or not s.get("writable", True)
        lines += [
            "", f"[{s['name']}]",
            f"   path = {root}/{s['name']}",
            "   browseable = yes",
            "   guest ok = yes",
            f"   read only = {'yes' if default_ro else 'no'}",
            f"   force user = {user}",
            f"   force group = {user}",
            "   create mask = 0664",
            "   directory mask = 0775",
            "   follow symlinks = no",
            "   wide links = no",
        ]
        if s.get("access") == "selected":
            ips = _sorted_ips(ip for mac, ip in ip_by_mac.items()
                              if share_mode(s, mac) != "none" and ip not in deny)
            lines.append(f"   hosts allow = 127.0.0.1 {' '.join(ips)}".rstrip())
            lines.append("   hosts deny = ALL")
        else:
            blocked_here = _sorted_ips(ip for mac, ip in ip_by_mac.items()
                                       if share_mode(s, mac) == "none")
            if blocked_here:
                extra = " ".join(sorted(set(deny) | set(blocked_here),
                                        key=lambda a: ipaddress.ip_address(a)))
                base = "127.0.0.1 " + " ".join(nets) if nets else "127.0.0.1"
                lines.append(f"   hosts allow = {base} EXCEPT {extra}")
                lines.append("   hosts deny = ALL")
        lines.append(f"   include = {client_dir}/{s['name']}-%I.conf")
    return "\n".join(lines) + "\n"


def _global_block(text: str) -> list[str]:
    """The [global] lines of our config, minus ``hosts allow``/``hosts deny``
    (those are read per connection, so a reload is enough for them)."""
    out, on = [], False
    for line in text.splitlines():
        if line.startswith("["):
            on = line.strip() == "[global]"
            continue
        if on and line.strip() and not line.strip().startswith(("hosts allow", "hosts deny")):
            out.append(line.strip())
    return out


def _is_ipv4(value: Any) -> bool:
    try:
        return ipaddress.ip_address(str(value)).version == 4
    except ValueError:
        return False


class DiskShareManager:
    def __init__(self, run_command: RunCommand | None = None, root: str = SHARE_ROOT,
                 smb_shares: str = SMB_SHARES, smb_main: str = SMB_MAIN,
                 client_dir: str = SMB_CLIENT_DIR,
                 which: Callable[[str], str | None] | None = None,
                 cred_dir: str = CRED_DIR,
                 ismount: Callable[[str], bool] = os.path.ismount,
                 lan_if: str = "", allowed_nets: Iterable[str] = (),
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._run = run_command or _default_run
        self.root = root
        self.smb_shares = smb_shares
        self.smb_main = smb_main
        self.client_dir = client_dir
        # /usr/sbin is not always on the service's PATH.
        self._which = which or (lambda b: shutil.which(
            b, path=os.environ.get("PATH", "") + ":/usr/sbin:/sbin"))
        self.cred_dir = cred_dir
        self._ismount = ismount
        self.lan_if = lan_if if _IFACE_RE.match(lan_if or "") else ""
        self.allowed_nets = ipv4_networks(allowed_nets)
        self._clock = clock
        self._lock = threading.Lock()
        self._installing = False
        self._install_error = ""
        self._live: list[dict[str, Any]] = []      # shares currently published
        self._denied: frozenset[str] = frozenset()
        self._ip_by_mac: dict[str, str] = {}
        self._net_retry: dict[str, float] = {}     # share name -> next retry time
        self._uidgid: tuple[int, int] | None = None
        self._mounted_rw: dict[str, bool] = {}     # share name -> mounted read-write
        # smbd binds the addresses present when IT starts; at boot that can be
        # before the gateway adds its client address (192.168.2.1), so the
        # first write after the app starts always restarts smbd.
        self._smbd_rebound = False

    # ---------------------------------------------------------------- state
    def samba_installed(self) -> bool:
        return bool(self._which("smbd"))

    def cifs_installed(self) -> bool:
        return bool(self._which("mount.cifs"))

    def has_state(self) -> bool:
        """True when earlier shares may have left mounts or a Samba file."""
        return Path(self.smb_shares).exists() or Path(self.root).is_dir()

    def list_disks(self) -> list[dict[str, Any]]:
        rc, out = self._run(["lsblk", "-J", "-b", "-o",
                             "NAME,PATH,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,TYPE,RM,MODEL"])
        if rc != 0:
            log.warning("lsblk failed: %s", out)
            return []
        try:
            return disks_from_lsblk(json.loads(out), self.root)
        except ValueError:
            return []

    def status(self, shares: list[dict[str, Any]]) -> dict[str, Any]:
        disks = self.list_disks()
        by_uuid = {d["uuid"]: d for d in disks}
        view = []
        for s in shares:
            target = self.target(s)
            if s.get("source") == "network":
                active = self._ismount(target)
                view.append({**s, "present": active, "active": active, "disk": None})
                continue
            d = by_uuid.get(s["uuid"])
            view.append({**s, "present": d is not None,
                         "active": bool(d and d["mounted"] == target),
                         "disk": d})
        return {"samba": self.samba_installed(), "cifs": self.cifs_installed(),
                "installing": self._installing, "install_error": self._install_error,
                "lan_only": bool(self.lan_if), "disks": disks, "shares": view}

    def target(self, s: dict[str, Any]) -> str:
        return f"{self.root}/{s['name']}"

    # ---------------------------------------------------------------- apply
    def apply(self, shares: list[dict[str, Any]], force_network: bool = True) -> list[str]:
        """Mount every share, write the Samba config and (re)load Samba.
        Unmounts our folders that are no longer shared. Returns problems.
        ``force_network`` retries unreachable network folders now instead of
        waiting for their back-off (an explicit admin action)."""
        with self._lock:
            problems: list[str] = []
            wanted = {s["name"] for s in shares}
            disks = {d["uuid"]: d for d in self.list_disks()}
            root = Path(self.root)
            if root.is_dir():
                for child in root.iterdir():
                    if child.name not in wanted and self._ismount(str(child)):
                        self._run(["umount", "-l", str(child)])
            live = []
            for s in shares:
                target = self.target(s)
                if s.get("source") == "network":
                    problem = self._mount_network(s, target, force_network)
                else:
                    problem = self._mount_disk(s, target, disks.get(s["uuid"]))
                if problem:
                    problems.append(f"{s['name']}: {problem}")
                else:
                    live.append(s)
            for name in list(self._net_retry):
                if name not in wanted:
                    del self._net_retry[name]
            self._write_samba(live)
            for p in problems:
                log.warning("disk share: %s", p)
            return problems

    def heal(self, shares: list[dict[str, Any]]) -> bool:
        """Called every minute: unmount disks that were unplugged, remount the
        ones that came back, retry unreachable network folders after their
        back-off. Re-applies only when something changed; True if it did."""
        if not shares:
            return False
        disks = {d["uuid"]: d for d in self.list_disks()}
        now = self._clock()
        stale, wanted_live = [], []
        for s in shares:
            target = self.target(s)
            if s.get("source") == "network":
                up = self._ismount(target)
                if up:
                    wanted_live.append(s["name"])
                elif now >= self._net_retry.get(s["name"], 0):
                    stale.append(s["name"])      # down, and its back-off is over
                continue
            d = disks.get(s["uuid"])
            if d is None:
                if self._ismount(target):
                    # unplugged while mounted: drop the dead mount now
                    self._run(["umount", "-l", target])
                    stale.append(s["name"])
                elif s["name"] in self._names(self._live):
                    stale.append(s["name"])
            elif d["mounted"] != target:
                stale.append(s["name"])          # plugged back in
            else:
                wanted_live.append(s["name"])
        if not stale and set(wanted_live) == self._names(self._live):
            return False
        self.apply(shares, force_network=False)
        return True

    @staticmethod
    def _names(shares: list[dict[str, Any]]) -> set[str]:
        return {s["name"] for s in shares}

    def set_clients(self, denied: Iterable[str],
                    ip_by_mac: dict[str, str] | None = None) -> bool:
        """Feed the current devices: ``denied`` are the IPs the gateway cuts
        off (they lose every share), ``ip_by_mac`` maps each device to its
        current IP for the per-device levels. Cache-gated: the Samba config is
        rewritten only when either actually changed."""
        new = frozenset(d for d in denied or [] if _is_ipv4(d))
        ips = {str(m).lower(): ip for m, ip in (ip_by_mac or {}).items()
               if _MAC_RE.match(str(m).lower()) and _is_ipv4(ip)}
        if new == self._denied and ips == self._ip_by_mac:
            return False
        self._denied = new
        self._ip_by_mac = ips
        with self._lock:
            if self._live or Path(self.smb_shares).exists():
                self._write_samba(self._live, reload_only=True)
        return True

    # ------------------------------------------------------------ mounting
    def _share_ids(self) -> tuple[int, int]:
        """uid/gid of the dedicated share user, creating it on first use."""
        if self._uidgid is not None:
            return self._uidgid
        rc, out = self._run(["id", "-u", SHARE_USER])
        if rc != 0:
            self._run(["useradd", "--system", "--no-create-home", "--user-group",
                       "--shell", "/usr/sbin/nologin", SHARE_USER])
        rc_u, uid = self._run(["id", "-u", SHARE_USER])
        rc_g, gid = self._run(["id", "-g", SHARE_USER])
        try:
            self._uidgid = (int(uid), int(gid)) if rc_u == 0 and rc_g == 0 else (0, 0)
        except ValueError:
            self._uidgid = (0, 0)
        return self._uidgid

    def _remount_if_mode_changed(self, s: dict[str, Any], target: str) -> bool:
        """True when the share is mounted at the right level (nothing to do).
        A share that now needs read-write (or no longer does) is unmounted so
        it can be mounted again at the new level."""
        known = self._mounted_rw.get(s["name"])
        if known is None or known == share_needs_rw(s):
            return True
        self._run(["umount", "-l", target])
        self._mounted_rw.pop(s["name"], None)
        return False

    def _mount_disk(self, s: dict[str, Any], target: str, d: dict[str, Any] | None) -> str:
        if d is None:
            return "disk not connected"
        if d["mounted"] == target and self._remount_if_mode_changed(s, target):
            return ""
        if d["mounted"] == target:
            d = {**d, "mounted": ""}
        if d["mounted"]:
            return f"disk is already mounted at {d['mounted']}"
        os.makedirs(target, exist_ok=True)
        opts = []
        if d["fstype"] in NON_POSIX_FS:
            uid, gid = self._share_ids()
            opts.append(f"uid={uid},gid={gid},umask=002")
        if not share_needs_rw(s):
            opts.append("ro")
        argv = ["mount", "-U", s["uuid"], target]
        if opts:
            argv[1:1] = ["-o", ",".join(opts)]
        rc, out = self._run(argv)
        if rc != 0:
            return f"mount failed: {out}"
        self._mounted_rw[s["name"]] = share_needs_rw(s)
        if d["fstype"] in POSIX_FS and share_needs_rw(s):
            # Linux filesystems keep their own owners: hand the share's top
            # folder to the share user so new files can be written there.
            uid, gid = self._share_ids()
            self._run(["chown", f"{uid}:{gid}", target])
            self._run(["chmod", "0775", target])
        return ""

    # ------------------------------------------------------------- network
    def cred_file(self, name: str) -> str:
        return os.path.join(self.cred_dir, name)

    def save_credentials(self, name: str, user: str, password: str) -> None:
        """Keep a network folder's login in a root-only file (never in the DB)."""
        # One option per line: a line break would add a mount option of the
        # caller's choosing (domain=, uid=...) to the credentials file.
        if any(ch in (user or "") + (password or "") for ch in "\r\n\x00"):
            raise ValueError("user name and password cannot contain line breaks")
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

    def _mount_network(self, s: dict[str, Any], target: str, force: bool = True) -> str:
        if self._ismount(target) and self._remount_if_mode_changed(s, target):
            return ""
        if not force and self._clock() < self._net_retry.get(s["name"], 0):
            return f"{s['remote']} unreachable, retrying later"
        os.makedirs(target, exist_ok=True)
        uid, gid = self._share_ids()
        opts = [f"uid={uid}", f"gid={gid}", "file_mode=0664", "dir_mode=0775"]
        if s.get("user"):
            opts.append(f"credentials={self.cred_file(s['name'])}")
        else:
            opts.append("guest")
        if not share_needs_rw(s):
            opts.append("ro")
        # Bounded: an unreachable host must never hold up startup or the API.
        rc, out = self._run(["timeout", str(CIFS_MOUNT_TIMEOUT_S), "mount", "-t", "cifs",
                             s["remote"], target, "-o", ",".join(opts)],
                            timeout=CIFS_MOUNT_TIMEOUT_S + 5)
        if rc != 0:
            self._net_retry[s["name"]] = self._clock() + NETWORK_RETRY_S
            return f"could not reach {s['remote']}: {out or f'exit {rc}'}"
        self._net_retry.pop(s["name"], None)
        self._mounted_rw[s["name"]] = share_needs_rw(s)
        return ""

    # --------------------------------------------------------------- samba
    def _write_samba(self, shares: list[dict[str, Any]], reload_only: bool = False) -> None:
        if not self.samba_installed():
            return
        text = render_smb(shares, self.root, self.lan_if, self.allowed_nets,
                          self._denied, SHARE_USER, self._ip_by_mac, self.client_dir)
        clients = render_client_overrides(shares, self._ip_by_mac, self._denied)
        try:
            old = Path(self.smb_shares).read_text(encoding="utf-8") \
                if Path(self.smb_shares).exists() else ""
            # Which interfaces smbd listens on is read only at start: a change
            # to [global] (first share, new LAN NIC) needs a real restart.
            restart = _global_block(old) != _global_block(text) or \
                (bool(shares) and not self._smbd_rebound)
            Path(self.smb_shares).write_text(text, encoding="utf-8")
            self._sync_client_files(clients)
            self._ensure_include()
        except OSError as exc:
            log.error("could not write the Samba config: %s", exc)
            return
        self._live = list(shares)
        if shares:
            self._run(["systemctl", "enable", "--now", "smbd"])
        if restart:
            self._run(["systemctl", "restart", "smbd"])
            self._smbd_rebound = True
        elif reload_only:
            self._run(["smbcontrol", "smbd", "reload-config"])
        else:
            self._run(["systemctl", "reload", "smbd"])

    def _sync_client_files(self, files: dict[str, str]) -> None:
        folder = Path(self.client_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.conf"):
            if old.name not in files:
                old.unlink()
        for name, content in files.items():
            path = folder / name
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                path.write_text(content, encoding="utf-8")

    def _ensure_include(self) -> None:
        """Our file must be included FIRST so its [global] options cover every
        share; an include left at the end by an older version is moved."""
        main = Path(self.smb_main)
        text = main.read_text(encoding="utf-8") if main.exists() else "[global]\n"
        include = f"include = {self.smb_shares}"
        head = f"{INCLUDE_COMMENT}\n{include}\n"
        if text.startswith(head):
            return
        kept = [line for line in text.splitlines()
                if line.strip() != include and line.strip() != INCLUDE_COMMENT
                and line.strip() != "# Quota Manager disk shares"]
        main.write_text(head + "\n".join(kept).lstrip("\n") + "\n", encoding="utf-8")

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
                                 "apt-get", "install", "-y", "--no-install-recommends",
                                 "samba", "cifs-utils"], timeout=900)
            if rc != 0:
                self._install_error = (out or f"exit {rc}")[-300:]
        finally:
            self._installing = False
