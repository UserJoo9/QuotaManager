"""WAN-topology detection for the dashboard WAN tab.

In default (LAN) mode the box sits behind the router and the client subnet is
the only thing it owns. In strong (WAN) mode the box dials the PPPoE line
itself and ``ppp0`` carries the public IP — a static-IP device then has no
second router to bypass through. This module only *detects* whether a PPP
interface is up; the actual bring-up is done by the setup script
(``QUOTA_TOPOLOGY=wan``) and the dashboard toggle only persists the preference.

Everything here is best-effort and never raises: no root, no ``ip``, or no
``/sys`` (Windows dev box, tests) degrades to a safe ``"unknown"`` so the rest
of the app keeps working (mirrors the engine's graceful degradation).
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger("quota.topology")

RunCommand = Callable[[list[str]], tuple[int, str]]


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "")


def detect_ppp(interface: str = "ppp0",
               run_command: RunCommand | None = None,
               sysfs_root: str = "/sys/class/net") -> dict[str, str]:
    """Report whether ``interface`` (a PPP link, default ``ppp0``) is up.

    Returns ``{"state": ..., "local": ..., "peer": ...}`` where ``state`` is
    ``"up"``, ``"down"``, or ``"unknown"`` (ip/sysfs unreadable — no root, no
    ``ip`` binary, or Windows). ``local``/``peer`` are the PPP address pair from
    ``ip -o -4 addr show dev <iface>`` (empty when not readable). The
    ``run_command``/``sysfs_root`` injections keep the test deterministic.

    NOTE: a PPP link is judged by its negotiated point-to-point IPv4 address,
    NOT by ``operstate``. PPP interfaces are carrier-less — the kernel reports
    ``/sys/class/net/ppp0/operstate`` as ``unknown`` (or ``down``) even while
    pppd is connected with a live IP, so a fast path that trusted operstate
    read a dialed-up line as down (the v19.8 box report: pppd "local IP
    197.121.113.253" while the WAN tab read "ppp0 down / Public IP —").
    """
    run = run_command or _default_run_command
    # Only the interface's existence is reliable from sysfs for ppp; its carrier
    # state is not.
    exists = (Path(sysfs_root) / interface).is_dir()

    code, out = run(["ip", "-o", "-4", "addr", "show", interface])
    if code == 0:
        # 7: ppp0    inet 100.64.0.2 peer 100.64.0.1/32 scope global ppp0
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)(?: peer (\d+\.\d+\.\d+\.\d+))?",
                      out)
        if m:
            return {"state": "up", "local": m.group(1), "peer": m.group(2) or ""}
        # Interface present but carries no IPv4 yet — discovery/LCP in progress.
        return {"state": "down", "local": "", "peer": ""}
    if exists:
        # sysfs still lists the interface but `ip` cannot show an address.
        return {"state": "down", "local": "", "peer": ""}
    return {"state": "unknown", "local": "", "peer": ""}


def check_internet(hosts: tuple[str, ...] = ("1.1.1.1", "8.8.8.8"),
                   port: int = 443,
                   timeout: float = 2.0,
                   connect: Callable | None = None) -> bool:
    """Prove the gateway can reach the public internet (the WAN-tab green dot).

    Raw-IP TCP connect — deliberately NOT ICMP (needs root/setuid) and NOT DNS
    (a resolver failure must not false-negative when routing is fine). Returns
    True when ANY host accepts a connection; a closed/filtered port, no route,
    or a timeout all mean "not reachable now". ``connect`` is injectable for
    tests (default :func:`socket.create_connection`), so a run-wiring test can
    fake the network instead of dialing out.
    """
    connect = connect or socket.create_connection
    for host in hosts:
        try:
            with connect((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False
