"""Transactional updates and live verification for the gateway dnsmasq binding."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from quota.lan_interface import LanSelection

log = logging.getLogger(__name__)
RunCommand = Callable[[list[str]], tuple[int, str]]


class DnsmasqError(RuntimeError):
    """The managed dnsmasq fragment could not be applied and verified."""


@dataclass(frozen=True)
class DhcpBinding:
    healthy: bool
    ipv4_interface: str = ""
    ipv6_interface: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class DnsmasqPaths:
    target: Path
    conf_dir: Path
    backup_dir: Path


@dataclass(frozen=True)
class FragmentUpdate:
    path: Path
    content: str | None


@dataclass
class DnsmasqChange:
    changed: bool
    binding: DhcpBinding
    quarantined: int = 0
    _rollback_action: Callable[[], bool] = field(default=lambda: True, repr=False)

    def rollback(self) -> bool:
        """Restore the staged dnsmasq files and restart the old config."""
        return self._rollback_action()


_LEGACY_BACKUPS = ("quota-gateway.conf.bak*", "quota-gateway.conf~")
def _command(run: RunCommand, argv: list[str]) -> tuple[int, str]:
    try:
        return run(argv)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _current_invocation(run: RunCommand) -> str:
    code, out = _command(run, [
        "systemctl", "show", "dnsmasq", "--property=InvocationID", "--value",
    ])
    return out.strip() if code == 0 else ""


def _journal_binding(run: RunCommand, iface: str) -> bool:
    invocation = _current_invocation(run)
    if not invocation:
        return False
    code, out = _command(run, [
        "journalctl", "-u", "dnsmasq", f"--invocation={invocation}",
        "-o", "cat", "--no-pager",
    ])
    return (code == 0 and
            f"sockets bound exclusively to interface {iface}" in out)


def _socket_ports(run: RunCommand) -> tuple[str, bool, bool]:
    code, out = _command(run, ["ss", "-H", "-lunp"])
    if code != 0:
        return "", False, False
    v4_line = next((line for line in out.splitlines()
                    if "dnsmasq" in line and re.search(r":67\s", line)), "")
    v6_line = next((line for line in out.splitlines()
                    if "dnsmasq" in line and re.search(r":547\s", line)), "")
    return out, bool(v4_line), bool(v6_line)


def probe_dhcp_binding(*, run: RunCommand, expect_ipv6: bool,
                       expected_interface: str = "") -> DhcpBinding:
    """Inspect the live daemon sockets and this process invocation's bind log."""
    code, _ = _command(run, ["systemctl", "is-active", "--quiet", "dnsmasq"])
    if code != 0:
        return DhcpBinding(False, evidence="dnsmasq is not active")
    sockets, has_v4, has_v6 = _socket_ports(run)
    if not has_v4 or (expect_ipv6 and not has_v6):
        return DhcpBinding(False, evidence="dnsmasq DHCP socket ports are missing")

    v4 = re.search(r"0\.0\.0\.0%([^:\s]+):67\s+.*dnsmasq", sockets)
    v6 = re.search(r"\[::\]%([^:\s]+):547\s+.*dnsmasq", sockets)
    journal_ok = bool(expected_interface) and _journal_binding(run, expected_interface)
    v4_iface = v4.group(1) if v4 else (expected_interface if journal_ok else "")
    v6_iface = v6.group(1) if v6 else (expected_interface if journal_ok else "")
    if expected_interface:
        healthy = v4_iface == expected_interface
        if expect_ipv6:
            healthy = healthy and v6_iface == expected_interface
    else:
        healthy = bool(v4_iface) and (not expect_ipv6 or bool(v6_iface))
    evidence = "interface-scoped DHCP sockets" if v4 and (v6 or not expect_ipv6) \
        else "current invocation exclusive-bind log and DHCP ports"
    return DhcpBinding(healthy, v4_iface, v6_iface if expect_ipv6 else "", evidence)


def _replace_interface(text: str, interface: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    found = False
    for line in lines:
        if line.lstrip().startswith("interface="):
            if not found:
                output.append(f"interface={interface}")
                found = True
            continue
        output.append(line)
    if not found:
        output.insert(0, f"interface={interface}")
    return "\n".join(output) + "\n"


def _write_atomic(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _snapshot(path: Path) -> tuple[bytes | None, int]:
    if not path.exists():
        return None, 0o644
    return path.read_bytes(), path.stat().st_mode & 0o777


def _restore(snapshot: dict[Path, tuple[bytes | None, int]]) -> bool:
    okay = True
    for path, (content, mode) in reversed(list(snapshot.items())):
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _write_atomic(path, content, mode)
        except OSError:
            log.exception("could not restore managed dnsmasq file %s", path)
            okay = False
    return okay


def _quarantine_legacy(paths: DnsmasqPaths) -> list[tuple[Path, Path]]:
    paths.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.backup_dir, 0o700)
    moved: list[tuple[Path, Path]] = []
    for pattern in _LEGACY_BACKUPS:
        for old in paths.conf_dir.glob(pattern):
            destination = paths.backup_dir / (
                f"{old.name}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
            )
            shutil.move(str(old), str(destination))
            moved.append((old, destination))
    return moved


def _has_legacy(paths: DnsmasqPaths) -> bool:
    return any(path for pattern in _LEGACY_BACKUPS
               for path in paths.conf_dir.glob(pattern))


def reconcile_dnsmasq(selection: LanSelection, *, replacement: str | None,
                      ipv6_fragment: FragmentUpdate | None, expect_ipv6: bool,
                      force_restart: bool, paths: DnsmasqPaths,
                      run: RunCommand) -> DnsmasqChange:
    """Validate, install and verify dnsmasq configuration with rollback."""
    before: dict[Path, tuple[bytes | None, int]] = {}
    moved: list[tuple[Path, Path]] = []
    if paths.target.exists():
        before[paths.target] = _snapshot(paths.target)
    if ipv6_fragment is not None:
        before[ipv6_fragment.path] = _snapshot(ipv6_fragment.path)
    old_text = paths.target.read_text(encoding="utf-8") if paths.target.exists() else ""
    target_text = _replace_interface(replacement if replacement is not None else old_text,
                                     selection.name)
    interface_changed = target_text != old_text
    fragment_changed = False
    if ipv6_fragment is not None:
        current = before[ipv6_fragment.path][0]
        desired = ipv6_fragment.content.encode() if ipv6_fragment.content is not None else None
        fragment_changed = current != desired
    existing = probe_dhcp_binding(run=run, expect_ipv6=expect_ipv6,
                                  expected_interface=selection.name)
    changed = (interface_changed or fragment_changed or force_restart
               or _has_legacy(paths))
    if not changed and existing.healthy:
        return DnsmasqChange(False, existing)

    restart_attempted = False
    try:
        moved = _quarantine_legacy(paths)
        _write_atomic(paths.target, target_text.encode(), before.get(paths.target, (None, 0o644))[1])
        if ipv6_fragment is not None and fragment_changed:
            if ipv6_fragment.content is None:
                ipv6_fragment.path.unlink(missing_ok=True)
            else:
                _write_atomic(ipv6_fragment.path, ipv6_fragment.content.encode(),
                              before[ipv6_fragment.path][1])
        code, out = _command(run, ["dnsmasq", "--test"])
        if code != 0:
            raise DnsmasqError(f"effective dnsmasq config failed validation: {out.strip()}")
        code, out = _command(run, ["systemctl", "restart", "dnsmasq"])
        restart_attempted = True
        if code != 0:
            raise DnsmasqError(f"dnsmasq restart failed: {out.strip()}")
        verified = probe_dhcp_binding(run=run, expect_ipv6=expect_ipv6,
                                      expected_interface=selection.name)
        if not verified.healthy:
            raise DnsmasqError(f"dnsmasq binding verification failed: {verified.evidence}")
    except Exception as exc:
        restored = _restore(before)
        # Legacy backups may contain duplicate dnsmasq directives. Keep them
        # quarantined even if the new managed fragment has to roll back; moving
        # them back would reintroduce the condition that this repair removes.
        if not restored:
            log.error("dnsmasq rollback was incomplete")
        if restart_attempted:
            code, _ = _command(run, ["dnsmasq", "--test"])
            if code == 0:
                code, _ = _command(run, ["systemctl", "restart", "dnsmasq"])
            if code != 0:
                restored = False
            previous_interface = re.search(r"^\s*interface=([^\s#]+)", old_text,
                                           re.MULTILINE)
            if previous_interface and code == 0:
                previous = probe_dhcp_binding(
                    run=run, expect_ipv6=expect_ipv6,
                    expected_interface=previous_interface.group(1))
                restored = restored and previous.healthy
        if not restored:
            raise DnsmasqError(
                f"{exc}; restoring the previous dnsmasq service state was incomplete"
            ) from exc
        raise

    def rollback() -> bool:
        okay = _restore(before)
        if okay:
            code, _ = _command(run, ["dnsmasq", "--test"])
            okay = code == 0
        if okay:
            code, _ = _command(run, ["systemctl", "restart", "dnsmasq"])
            okay = code == 0
        return okay

    return DnsmasqChange(True, verified, len(moved), rollback)
