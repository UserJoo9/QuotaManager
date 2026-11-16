"""OS detection + service-control helpers (systemd vs OpenWrt/procd).

Kept tiny and dependency-free. On systemd hosts every command is exactly what the
code used before (``systemctl ...``), so behaviour there is unchanged. On OpenWrt
(no systemd) the same call maps to ``/etc/init.d/<service>``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Our own service is called quota-gateway on systemd, quota-manager on OpenWrt.
_OPENWRT_NAMES = {"quota-gateway": "quota-manager"}


def is_openwrt(root: str = "/") -> bool:
    try:
        return (Path(root) / "etc" / "openwrt_release").exists()
    except OSError:
        return False


def has_systemd(root: str = "/") -> bool:
    return (Path(root) / "run" / "systemd" / "system").is_dir()


def service_cmd(action: str, name: str, *, quiet: bool = False,
                openwrt: bool | None = None) -> list[str]:
    """argv for ``systemctl <action> <name>`` or its OpenWrt equivalent.

    ``is-active`` has no init.d twin, so on OpenWrt it maps to ``init.d status``
    (exit 0 = running).
    """
    if openwrt is None:
        openwrt = is_openwrt() and not has_systemd()
    if not openwrt:
        cmd = ["systemctl", action]
        if quiet:
            cmd.append("--quiet")
        return cmd + [name]
    name = _OPENWRT_NAMES.get(name, name)
    verb = {"try-restart": "restart", "is-active": "running"}.get(action, action)
    return [f"/etc/init.d/{name}", verb]


def default_data_dir() -> str:
    return "/overlay/quota-gateway" if is_openwrt() else "/var/lib/quota-gateway"


def is_tmpfs(path: str) -> bool:
    """True when *path* lives on a RAM filesystem (data would vanish on reboot)."""
    try:
        real = os.path.realpath(path)
        best, fstype = "", ""
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1]
                if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) \
                        and len(mnt) >= len(best):
                    best, fstype = mnt, parts[2]
        return fstype in ("tmpfs", "ramfs")
    except OSError:
        return False


def have(binary: str) -> bool:
    return shutil.which(binary) is not None
