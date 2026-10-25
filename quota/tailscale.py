"""Built-in remote access through Tailscale.

The gateway sits behind an ISP line that often cannot be reached from the
internet directly (carrier-grade NAT, filtered ports, lossy paths).
Tailscale gives the admin a private, encrypted address for the dashboard that
works from anywhere without opening a port on the public IP.

This module only *manages* the official ``tailscale`` client already
installed on the box: it reads its state, can install it with the vendor's
own script, starts a login (returning the sign-in link), and disconnects.
Nothing here ever raises into the caller — every method returns a plain dict
the dashboard can render, with a ``state`` and a human ``message``.

``state`` is one of:

* ``not_installed`` — the ``tailscale`` binary is missing
* ``installing``    — the installer is running
* ``daemon_down``   — installed, but the ``tailscaled`` service is not running
* ``needs_login``   — waiting for the admin to sign in (``auth_url`` is set)
* ``stopped``       — signed in but switched off
* ``starting``      — connecting
* ``connected``     — online; ``ip`` / ``dns_name`` are the dashboard address
* ``error``         — the last action failed (``message`` says why)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

log = logging.getLogger("quota.tailscale")

#: Network interface the tailnet traffic arrives on (also used by the firewall).
INTERFACE = "tailscale0"
DEFAULT_HOSTNAME = "quota-gateway"
INSTALL_COMMAND = "curl -fsSL https://tailscale.com/install.sh | sh"

RunCommand = Callable[[list[str]], "tuple[int, str]"]

#: Friendly install progress: the dashboard shows these steps instead of the
#: installer's raw output.  ``install_step`` is how many are done (0..4).
INSTALL_STEPS = 4
_STEP_MARKERS = (
    ("tailscale-archive-keyring.gpg", 1),     # trusted package source added
    ("apt-get update", 2),                    # package list refreshed
    ("Unpacking tailscale", 3),               # package downloaded, being installed
    ("Setting up tailscale ", 4),             # package configured
)


def install_step_from(lines: list[str]) -> int:
    """Progress (0..INSTALL_STEPS) reached by the installer so far."""
    step = 0
    for line in lines:
        for marker, n in _STEP_MARKERS:
            if marker in line and n > step:
                step = n
    return step

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out"
    except OSError as e:  # noqa: BLE001
        return 126, str(e)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def parse_status(text: str) -> dict[str, Any]:
    """Parse ``tailscale status --json`` into the fields the dashboard needs.

    Pure and forgiving: unknown / missing keys fall back to empty values.
    """
    try:
        data = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {"backend": "", "ip": "", "dns_name": "", "hostname": "", "auth_url": "",
                "version": "", "peers": 0, "online": False, "peer_list": [], "health": []}
    if not isinstance(data, dict):
        data = {}
    me = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    ips = [i for i in (me.get("TailscaleIPs") or data.get("TailscaleIPs") or []) if isinstance(i, str)]
    ip4 = next((i for i in ips if _IPV4.match(i)), "")
    dns = str(me.get("DNSName") or "").rstrip(".")
    peers = data.get("Peer") if isinstance(data.get("Peer"), dict) else {}
    peer_list = []
    for p in peers.values():
        if not isinstance(p, dict):
            continue
        peer_list.append({
            "name": str(p.get("HostName") or str(p.get("DNSName") or "").split(".")[0]),
            "os": str(p.get("OS") or ""),
            "online": bool(p.get("Online")),
            # CurAddr is set only while a direct path is in use; a Relay name
            # means the traffic goes through Tailscale's relay servers.
            "path": "direct" if p.get("CurAddr") else ("relay" if p.get("Relay") else ""),
        })
    peer_list.sort(key=lambda d: (not d["online"], d["name"].lower()))
    return {
        "backend": str(data.get("BackendState") or ""),
        "ip": ip4,
        "dns_name": dns,
        "hostname": str(me.get("HostName") or ""),
        "auth_url": str(data.get("AuthURL") or ""),
        "version": str(data.get("Version") or ""),
        "peers": len(peers),
        "online": bool(me.get("Online")) if me else False,
        "peer_list": peer_list[:8],
        # Tailscale's own diagnosis (e.g. "cannot reach the coordination server")
        "health": [str(m)[:200] for m in (data.get("Health") or []) if isinstance(m, str)][:3],
    }


def state_from(backend: str, has_ip: bool) -> str:
    """Map Tailscale's ``BackendState`` to this module's ``state``."""
    if backend == "Running":
        return "connected" if has_ip else "starting"
    if backend in ("NeedsLogin", "NeedsMachineAuth"):
        return "needs_login"
    if backend == "Stopped":
        return "stopped"
    if backend in ("Starting",):
        return "starting"
    return "daemon_down" if not backend or backend == "NoState" else "starting"


class TailscaleManager:
    """Thin, thread-safe wrapper around the ``tailscale`` command line."""

    def __init__(self,
                 run_command: RunCommand | None = None,
                 which: Callable[[str], str | None] | None = None,
                 popen: Callable[..., Any] | None = None,
                 hostname: str = DEFAULT_HOSTNAME,
                 install_script: str = INSTALL_COMMAND,
                 on_system_change: Callable[[str], None] | None = None) -> None:
        self._run = run_command or _default_run_command
        self._which = which or shutil.which
        self._popen = popen or subprocess.Popen
        self.hostname = hostname
        self._install_script = install_script
        #: Called (never raises into us) after the client was installed or
        #: connected: installing a package / bringing up a tunnel touches
        #: kernel networking, so the gateway re-verifies its own rules.
        self._on_system_change = on_system_change
        self._lock = threading.Lock()
        self._proc: Any = None          # background ``tailscale up`` (waits for sign-in)
        self._installing = False
        self._install_log: list[str] = []
        self._install_step = 0
        self._last_error = ""

    # -- helpers -------------------------------------------------------------

    def binary(self) -> str:
        """Path of the tailscale client, '' when it is not installed."""
        return self._which("tailscale") or ""

    def _status_raw(self, binary: str) -> tuple[int, str]:
        return self._run([binary, "status", "--json"])

    def _snapshot(self, state: str, message: str = "", **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "state": state, "installed": state not in ("not_installed", "installing"),
            "message": message, "ip": "", "dns_name": "", "hostname": "",
            "auth_url": "", "version": "", "peers": 0,
            "install_step": self._install_step,
            "install_steps": INSTALL_STEPS,
            # raw installer output is only for troubleshooting a failed install
            "install_log": self._install_log[-12:] if self._last_error else [],
            "install_command": self._install_script,
            "interface": INTERFACE, "peer_list": [], "peers_online": 0, "health": [],
        }
        out.update(extra)
        return out

    # -- public API ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Current state of the client (cheap: one ``tailscale status`` call)."""
        if self._installing:
            return self._snapshot("installing")
        binary = self.binary()
        if not binary:
            return self._snapshot("not_installed", self._last_error)
        code, out = self._status_raw(binary)
        info = parse_status(out) if code == 0 else parse_status("")
        if code != 0 and not info["backend"]:
            return self._snapshot("daemon_down", (out or "").strip()[-200:])
        st = state_from(info["backend"], bool(info["ip"]))
        return self._snapshot(
            st, self._last_error if st == "error" else "",
            ip=info["ip"], dns_name=info["dns_name"], hostname=info["hostname"],
            auth_url=info["auth_url"] if st == "needs_login" else "",
            version=info["version"], peers=info["peers"],
            peer_list=info["peer_list"], health=info["health"],
            peers_online=sum(1 for p in info["peer_list"] if p["online"]))

    def install(self) -> dict[str, Any]:
        """Install the official client in the background (vendor script)."""
        with self._lock:
            if self._installing:
                return self.status()
            if self.binary():
                return self.status()
            self._installing = True
            self._install_log = []
            self._install_step = 0
            self._last_error = ""
        threading.Thread(target=self._install_worker, name="tailscale-install", daemon=True).start()
        return self._snapshot("installing")

    def _install_worker(self) -> None:
        try:
            proc = self._popen(["sh", "-c", self._install_script],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if proc.stdout is not None:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        self._install_log.append(line[-300:])
                        del self._install_log[:-60]
                        self._install_step = max(self._install_step, install_step_from([line]))
            rc = proc.wait()
            if rc != 0:
                self._last_error = f"install failed (exit {rc})"
            else:
                self._run(["systemctl", "enable", "--now", "tailscaled"])
                self._install_step = INSTALL_STEPS
                self._notify("installed")
        except Exception as e:  # noqa: BLE001
            log.exception("tailscale install failed")
            self._last_error = f"install failed: {e}"
        finally:
            self._installing = False

    def _notify(self, reason: str) -> None:
        cb = self._on_system_change
        if cb is None:
            return
        try:
            cb(reason)
        except Exception:  # noqa: BLE001 — a heal problem must not break the install
            log.exception("tailscale: system-change hook failed")

    def connect(self, wait: float = 8.0) -> dict[str, Any]:
        """Start (or resume) the connection.

        When the box is not signed in yet, returns ``state == "needs_login"``
        with ``auth_url``: the admin opens it once and approves the device.
        """
        binary = self.binary()
        if not binary:
            return self._snapshot("not_installed")
        with self._lock:
            self._last_error = ""
            cur = self.status()
            if cur["state"] == "connected":
                return cur
            if cur["state"] == "daemon_down":
                self._run(["systemctl", "enable", "--now", "tailscaled"])
                time.sleep(1.0)
            # (Re)start a background ``up``: it waits for the sign-in to finish.
            self._kill_proc()
            try:
                self._proc = self._popen(
                    [binary, "up", "--reset", "--accept-dns=false", "--accept-routes=false",
                     "--hostname", self.hostname],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:  # noqa: BLE001
                self._last_error = f"could not start tailscale: {e}"
                return self._snapshot("error", self._last_error)
        deadline = time.monotonic() + max(0.0, wait)
        snap = self.status()
        while snap["state"] not in ("connected", "needs_login") or \
                (snap["state"] == "needs_login" and not snap["auth_url"]):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
            snap = self.status()
        self._notify("connect")
        return snap

    def disconnect(self, logout: bool = False) -> dict[str, Any]:
        """Switch the tailnet connection off (``logout`` also forgets this box)."""
        binary = self.binary()
        if not binary:
            return self._snapshot("not_installed")
        with self._lock:
            self._kill_proc()
            code, out = self._run([binary, "logout" if logout else "down"])
            if code != 0:
                self._last_error = (out or "").strip()[-200:] or "tailscale command failed"
                return self._snapshot("error", self._last_error)
            self._last_error = ""
        return self.status()

    def _kill_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:  # noqa: BLE001
            pass
