"""Shared discovery and validation for the wired client-facing interface."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

RunCommand = Callable[[list[str]], tuple[int, str]]
_IFACE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_EXCLUDED_PREFIXES = ("ifb", "ppp", "tun", "tap", "veth", "docker", "virbr")
_NM_ACTIVE = {"connected", "connecting", "activating"}


class LanResolutionError(RuntimeError):
    """No unique, currently usable LAN interface could be proven."""


@dataclass(frozen=True)
class LanRequest:
    gateway_ip: str
    uplink_ip: str = ""
    last_applied: str = ""
    requested: str = ""
    wan_interface: str = ""


@dataclass(frozen=True)
class LanSelection:
    name: str
    ifindex: int
    reason: str


@dataclass(frozen=True)
class _Candidate:
    name: str
    ifindex: int
    addresses: frozenset[str]


def _valid_name(name: str) -> bool:
    return bool(_IFACE_NAME.fullmatch(name)) and name not in {".", ".."}


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _active_link(path: Path) -> int | None:
    if not path.is_dir():
        return None
    if _read_int(path / "type") != 1:
        return None
    if _read_int(path / "carrier") != 1:
        return None
    try:
        if path.joinpath("operstate").read_text(encoding="ascii").strip() != "up":
            return None
    except OSError:
        return None
    return _read_int(path / "ifindex")


def _ipv4_addresses(run: RunCommand) -> dict[str, set[str]]:
    code, output = run(["ip", "-j", "-4", "addr", "show"])
    if code != 0:
        return {}
    try:
        rows = json.loads(output or "[]")
    except (TypeError, json.JSONDecodeError):
        log.warning("could not parse IPv4 interface addresses during LAN discovery")
        return {}
    result: dict[str, set[str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("ifname"), str):
            continue
        name = row["ifname"]
        for addr in row.get("addr_info", []):
            local = addr.get("local") if isinstance(addr, dict) else None
            try:
                parsed = ipaddress.ip_address(local)
            except ValueError:
                continue
            if parsed.version == 4:
                result.setdefault(name, set()).add(str(parsed))
    return result


def _nm_active_devices(run: RunCommand) -> set[str]:
    try:
        code, output = run([
            "nmcli", "-t", "--escape", "no", "-f", "DEVICE,TYPE,STATE",
            "device", "status",
        ])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if code != 0:
        return set()
    active: set[str] = set()
    for line in output.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[1] == "ethernet" and fields[2].lower() in _NM_ACTIVE:
            active.add(fields[0])
    return active


def _default_route_device(run: RunCommand) -> str:
    code, output = run(["ip", "-j", "route", "show", "default"])
    if code != 0:
        return ""
    try:
        routes = json.loads(output or "[]")
    except (TypeError, json.JSONDecodeError):
        return ""
    for route in routes if isinstance(routes, list) else []:
        if isinstance(route, dict) and isinstance(route.get("dev"), str):
            return route["dev"]
    return ""


def _selection(candidates: list[_Candidate], reason: str) -> LanSelection:
    if len(candidates) != 1:
        names = ", ".join(sorted(item.name for item in candidates))
        message = (
            f"multiple active wired LAN interfaces remain ({names}); "
            "select the client-facing interface explicitly"
        )
        log.warning("LAN interface discovery is ambiguous: %s", message)
        raise LanResolutionError(message)
    selected = candidates[0]
    return LanSelection(selected.name, selected.ifindex, reason)


def resolve_lan(request: LanRequest, *, run: RunCommand,
                sysfs_net: Path = Path("/sys/class/net")) -> LanSelection:
    """Resolve a wired, carrier-up client link without selecting stale addresses."""
    if request.requested:
        if not _valid_name(request.requested):
            raise LanResolutionError("requested LAN interface has an invalid name")
        path = sysfs_net / request.requested
        ifindex = _active_link(path)
        if ifindex is None:
            raise LanResolutionError(
                f"requested LAN interface {request.requested} is absent, down, "
                "or has no carrier"
            )
        return LanSelection(request.requested, ifindex, "explicit")

    addresses = _ipv4_addresses(run)
    candidates: list[_Candidate] = []
    try:
        entries = list(sysfs_net.iterdir())
    except OSError as exc:
        raise LanResolutionError(f"cannot enumerate wired interfaces: {exc}") from exc
    for entry in entries:
        name = entry.name
        if (not _valid_name(name) or name == "lo"
                or name.startswith(_EXCLUDED_PREFIXES)
                or name == request.wan_interface):
            continue
        if (entry / "wireless").exists():
            continue
        ifindex = _active_link(entry)
        if ifindex is not None:
            candidates.append(_Candidate(name, ifindex,
                                         frozenset(addresses.get(name, set()))))
    if not candidates:
        log.warning("LAN interface discovery found no wired link with carrier")
        raise LanResolutionError("no active wired LAN interface with carrier was found")

    remaining = candidates
    selected = [item for item in remaining if request.gateway_ip in item.addresses]
    if selected:
        if len(selected) == 1:
            return _selection(selected, "gateway")
        remaining = selected

    active_nm = _nm_active_devices(run)
    selected = [item for item in remaining if item.name in active_nm]
    if selected:
        if len(selected) == 1:
            return _selection(selected, "networkmanager")
        remaining = selected

    if request.uplink_ip:
        selected = [item for item in remaining if request.uplink_ip in item.addresses]
        if selected:
            if len(selected) == 1:
                return _selection(selected, "uplink-address")
            remaining = selected

    default_device = _default_route_device(run)
    selected = [item for item in remaining if item.name == default_device]
    if selected:
        if len(selected) == 1:
            return _selection(selected, "default-route")
        remaining = selected

    if len(remaining) == 1:
        return _selection(remaining, "only-active-wired-interface")
    return _selection(remaining, "ambiguous-evidence")


def validate_lan(selection: LanSelection, *, gateway_cidr: str | None,
                 run: RunCommand,
                 sysfs_net: Path = Path("/sys/class/net")) -> None:
    """Recheck a selected interface's name, ifindex, link state and address."""
    if not _valid_name(selection.name):
        raise LanResolutionError("LAN interface has an invalid name")
    path = sysfs_net / selection.name
    current = _active_link(path)
    if current is None:
        raise LanResolutionError(
            f"LAN interface {selection.name} is absent, down, or has no carrier"
        )
    if current != selection.ifindex:
        raise LanResolutionError(
            f"LAN interface identity changed for {selection.name} "
            f"(expected ifindex {selection.ifindex}, found {current})"
        )
    if gateway_cidr:
        try:
            gateway_ip = str(ipaddress.ip_interface(gateway_cidr).ip)
        except ValueError as exc:
            raise LanResolutionError(f"invalid LAN gateway CIDR {gateway_cidr!r}") from exc
        if gateway_ip not in _ipv4_addresses(run).get(selection.name, set()):
            raise LanResolutionError(
                f"LAN interface {selection.name} does not carry gateway {gateway_ip}"
            )


def _system_run(argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve or validate a LAN interface")
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--gateway-ip", required=True)
    resolve.add_argument("--uplink-ip", default="")
    resolve.add_argument("--last-applied", default="")
    resolve.add_argument("--requested", default="")
    resolve.add_argument("--wan-interface", default="")
    resolve.add_argument("--sysfs-net", type=Path, default=Path("/sys/class/net"))
    validate = commands.add_parser("validate")
    validate.add_argument("--interface", required=True)
    validate.add_argument("--ifindex", required=True, type=int)
    validate.add_argument("--gateway-cidr", default=None)
    args = parser.parse_args()
    try:
        if args.command == "resolve":
            request = LanRequest(args.gateway_ip, args.uplink_ip,
                                 args.last_applied, args.requested,
                                 args.wan_interface)
            result = resolve_lan(request, run=_system_run,
                                 sysfs_net=args.sysfs_net)
            print(f"{result.name}\t{result.ifindex}")
            return 0
        validate_lan(LanSelection(args.interface, args.ifindex, "shell"),
                     gateway_cidr=args.gateway_cidr, run=_system_run)
        return 0
    except LanResolutionError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
