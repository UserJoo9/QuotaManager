"""Keep the PPPoE peer and systemd unit aligned with the selected WAN link."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

log = logging.getLogger("quota.ppp_binding")
RunCommand = Callable[[list[str]], tuple[int, str]]
_IFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


def _write_atomic(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _peer_text(text: str, interface: str) -> str:
    lines = text.splitlines()
    plugin = next((i for i, line in enumerate(lines)
                   if line.strip() == "plugin pppoe.so"), None)
    if plugin is None:
        raise ValueError("PPPoE peer has no plugin pppoe.so directive")
    target = next((i for i in range(plugin + 1, len(lines))
                  if lines[i].strip() and not lines[i].lstrip().startswith("#")), None)
    if target is None or not _IFACE.fullmatch(lines[target].strip()):
        raise ValueError("PPPoE peer has no valid interface line")
    lines[target] = interface
    return "\n".join(lines) + "\n"


def reconcile_ppp_interface(
    *,
    wan_interface: str,
    run: RunCommand,
    peer_path: Path = Path("/etc/ppp/peers/quota-wan"),
    service_path: Path = Path("/etc/systemd/system/quota-wan-ppp.service"),
) -> bool:
    """Update PPPoE's bare peer interface and reload its unit when necessary."""
    if not _IFACE.fullmatch(wan_interface):
        log.error("refusing invalid PPPoE interface name")
        return False
    if not peer_path.exists():
        log.warning("PPPoE peer file is missing; cannot bind WAN interface %s",
                    wan_interface)
        return False

    old_peer = peer_path.read_text(encoding="utf-8")
    try:
        new_peer = _peer_text(old_peer, wan_interface)
    except ValueError as exc:
        log.error("cannot update PPPoE peer for %s: %s", wan_interface, exc)
        return False
    old_service = service_path.read_text(encoding="utf-8") if service_path.exists() else None
    new_service = old_service
    if old_service is not None:
        new_service = re.sub(
            r"(?m)^Description=Quota Manager PPPoE WAN \(dial .+? -> ppp0\)$",
            f"Description=Quota Manager PPPoE WAN (dial {wan_interface} -> ppp0)",
            old_service,
        )
    peer_changed = new_peer != old_peer
    service_changed = new_service != old_service
    if not peer_changed and not service_changed:
        return True

    peer_mode = peer_path.stat().st_mode & 0o777
    service_mode = service_path.stat().st_mode & 0o777 if service_path.exists() else 0o644
    try:
        if peer_changed:
            _write_atomic(peer_path, new_peer, peer_mode)
        if service_changed and new_service is not None:
            _write_atomic(service_path, new_service, service_mode)
        if service_changed:
            code, output = run(["systemctl", "daemon-reload"])
            if code != 0:
                raise RuntimeError(output.strip() or "systemd daemon-reload failed")
        if service_path.exists():
            code, output = run(["systemctl", "restart", "quota-wan-ppp"])
            if code != 0:
                raise RuntimeError(output.strip() or "PPPoE service restart failed")
            code, output = run(["systemctl", "is-active", "--quiet",
                                "quota-wan-ppp"])
            if code != 0:
                raise RuntimeError(output.strip() or "PPPoE service is not active")
    except Exception as exc:  # noqa: BLE001 — restore both managed files
        try:
            if peer_changed:
                _write_atomic(peer_path, old_peer, peer_mode)
            if service_changed and old_service is not None:
                _write_atomic(service_path, old_service, service_mode)
            if service_changed:
                run(["systemctl", "daemon-reload"])
            if service_path.exists():
                run(["systemctl", "restart", "quota-wan-ppp"])
        except OSError:
            log.exception("could not roll back the PPPoE interface update")
        log.error("PPPoE interface update to %s failed: %s", wan_interface, exc)
        return False

    log.info("PPPoE peer bound to %s", wan_interface)
    return True
