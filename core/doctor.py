"""``python3 run.py --doctor``: pre-flight report for this host (Debian or OpenWrt).

Imports only the stdlib + core modules so it still runs when the web stack
(fastapi, uvicorn, ...) is NOT installed — which is exactly when it is needed.
"""
from __future__ import annotations

import os
import sys


def _load_cfg(config_path):
    from core import config as cfg_mod
    return cfg_mod.load_config(cfg_mod.resolve_config_path(config_path))


def run_doctor(config_path: str | None) -> int:
    """Print a pre-flight report (works on Debian and OpenWrt). Exit 1 on any
    hard problem so it can gate scripts."""
    import importlib
    from core import oscompat
    from pathlib import Path
    bad = 0

    def line(ok: bool | None, label: str, hint: str = "") -> None:
        nonlocal bad
        mark = {True: "OK  ", False: "FAIL", None: "WARN"}[ok]
        if ok is False:
            bad += 1
        print(f"[{mark}] {label}" + (f"  -> {hint}" if hint and not ok else ""))

    print(f"OS: {'OpenWrt' if oscompat.is_openwrt() else 'Linux'}  "
          f"python {sys.version.split()[0]}  root={os.geteuid() == 0}")
    line(os.geteuid() == 0, "running as root", "nft/tc/ip need root")
    for mod in ("fastapi", "uvicorn", "aiosqlite", "yaml", "pydantic", "websockets"):
        try:
            m = importlib.import_module(mod)
            line(True, f"python module {mod} {getattr(m, '__version__', '')}")
        except Exception as exc:  # noqa: BLE001
            line(False if mod != "websockets" else None, f"python module {mod}", str(exc))
    for b, hint, hard in (("nft", "apk add nftables", True),
                          ("ip", "apk add ip-full", True),
                          ("dnsmasq", "apk add dnsmasq-full", True),
                          ("tc", "apk add tc-full (speed limits)", False),
                          ("nmcli", "optional; not on OpenWrt", None)):
        ok = oscompat.have(b)
        line(ok if (ok or hard) else None, f"binary {b}", hint)
    if not oscompat.is_openwrt() or oscompat.have("systemctl"):
        line(oscompat.have("systemctl"), "systemctl (or OpenWrt shim)",
             "install scripts/systemctl.openwrt as /usr/bin/systemctl")
    try:
        cfg = _load_cfg(config_path)
        d = str(Path(cfg.db_path).parent)
        Path(d).mkdir(parents=True, exist_ok=True)
        line(os.access(d, os.W_OK), f"db dir writable: {d}")
        line(not oscompat.is_tmpfs(d), "db dir is persistent (not tmpfs)",
             "data would vanish on reboot — use /overlay on OpenWrt")
    except Exception as exc:  # noqa: BLE001
        line(False, "config.yaml loads", str(exc))
    print("RESULT:", "problems found" if bad else "all good")
    return 1 if bad else 0


