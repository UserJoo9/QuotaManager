"""quota/tun2socks.py — auto-provision the tun2socks bridge for VPN share.

A VPN client with a USERSpace netstack (v2rayN) never exposes a kernel
``tun`` device, so the policy-routing manager (``quota/vpnshare.py``) has
nothing to route the client subnet into. The standard bridge is
**tun2socks** (https://github.com/xjasonlyu/tun2socks): it opens a real
kernel ``tun0`` and pipes every packet through a local SOCKS proxy that the
VPN client itself listens on (v2rayN defaults to ``127.0.0.1:10808``).

tun2socks is NOT in the Kali/apt repositories, so this manager **downloads
and verifies the static binary itself** the first time VPN share is turned
on (a one-time fetch, gated and cache-gated so it never re-downloads), then
keeps a child ``tun2socks`` process running while VPN share is enabled and
the kernel still lacks a tunnel. Like ``VpnShareManager`` it is pure
subprocess/syscall — the maintenance loop calls :meth:`reconcile` from a
worker thread. A missing/incompatible tunnel never blackholes the subnet:
if the binary can't be installed or the proxy is absent, reconcile reports
an honest status and the routing manager simply stays ``no-interface``.

Supply chain: the download is a goreleaser ``.zip`` whose sha256 is PINNED
per architecture (the release API publishes per-asset digests). An unknown
architecture or a hash mismatch refuses to install — the manager never
executes an unverified binary. ``vpn_share.tun2socks: false`` disables the
whole auto-provisioner (users who run their own kernel-TUN client sing-box/
xray/WireGuard don't need it, and a second tun would confuse the tunnel
detector).
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("quota.tun2socks")

STATE_OFF = "off"
STATE_RUNNING = "running"
STATE_DOWNLOADING = "downloading"
STATE_NO_BINARY = "no-binary"
STATE_NO_PROXY = "no-proxy"
STATE_ERROR = "error"

#: GitHub release the auto-provisioner pins (supply chain — a version bump
#: means updating this + ARCH_SHA256, not silently pulling "latest").
RELEASE_TAG = "v2.7.0"
RELEASE_BASE = (
    "https://github.com/xjasonlyu/tun2socks/releases/download/"
    f"{RELEASE_TAG}/tun2socks-"
)

#: per-arch release asset suffix -> pinned sha256 of that .zip. Published by
#: the GitHub release API as the asset's ``digest``. Empty = not pinned.
ARCH_SUFFIX = {
    "x86_64": "linux-amd64",
    "amd64": "linux-amd64",
    "aarch64": "linux-arm64",
    "arm64": "linux-arm64",
}
ARCH_SHA256 = {
    "linux-amd64": "a612baa287a3b6de6221f74fd02b442a50888508227ecf51e1288a5ccbb77381",
    "linux-arm64": "3931476c9cfa8fa236d23aeaf36767df0eb27cc11ecaab699faba57744450f49",
}

#: VPN-client process names whose LOCAL listener becomes the tun2socks proxy.
_PROXY_RE = re.compile(r"v2ray|sing[-_ ]?box|xray", re.IGNORECASE)

#: how long reconcile remembers a freshly crashed child before respawning it
#: (avoids a spawn loop when the proxy is dead).
RESPAWN_GATE = 10.0
#: how long between binary-download attempts (a slow/failed download must not
#: hit GitHub every 15 s tick).
RETRY_GATE = 60.0


@dataclass
class Tun2socksStatus:
    """What the dashboard shows for the auto-provisioned tun2socks bridge."""

    state: str = STATE_OFF       # one of STATE_*
    message: str = ""
    #: the SOCKS proxy the bridge is wired to (empty when not running).
    proxy: str = ""
    #: the tunnel interface tun2socks created (empty when not running).
    interface: str = ""


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "")


def _default_download(url: str, sha256: str, dest: Path) -> tuple[bool, str]:
    """Fetch a ``.zip`` release asset, verify its sha256, unzip the binary
    and atomically install it at ``dest`` (chmod +x). Returns (ok, message)."""
    import tempfile

    tmpdir = None
    try:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            zippath = tmpdir / "tun2socks.zip"
            log.info("tun2socks: downloading %s", url)
            urllib.request.urlretrieve(url, zippath)
            h = hashlib.sha256()
            with zippath.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            actual = h.hexdigest()
            if sha256 and actual.lower() != sha256.lower():
                return False, (f"sha256 mismatch (got {actual[:12]}…, "
                               f"expected {sha256[:12]}…)")
            with zipfile.ZipFile(zippath) as zf:
                member = next(
                    (n for n in zf.namelist()
                     # goreleaser names the binary `tun2socks-<os>-<arch>`
                     # (e.g. tun2socks-linux-amd64) — accept any `tun2socks*`.
                     if os.path.basename(n).startswith("tun2socks")),
                    None)
                if member is None:
                    return False, "archive has no tun2socks binary"
                data = zf.read(member)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmppath = dest.with_name(dest.name + ".tmp")
            tmppath.write_bytes(data)
            os.chmod(tmppath, 0o755)
            os.replace(tmppath, dest)
            return True, f"installed tun2socks {RELEASE_TAG}"
    except urllib.error.URLError as exc:
        return False, f"download failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"install failed: {exc}"


class _Child:
    """Tiny wrapper around a spawned process (pollable, terminable)."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc

    def poll(self) -> int | None:
        return self._proc.poll()

    def stop(self) -> None:
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except OSError:
                    pass


def _default_spawn(argv: list[str]) -> _Child:
    return _Child(subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True))


def _default_proxy_probe(proxy: str) -> bool:
    """Is the SOCKS endpoint accepting TCP connections right now?

    The bridge MUST NOT be spawned into a dead or made-up port — tun2socks
    then runs happily while every device packet disappears into it (a silent
    internet blackhole for the whole client subnet). Cheap 1 s connect to
    the local endpoint; the proxy server accepts the TCP connection before
    any SOCKS handshake, so a successful connect means a live listener."""
    try:
        host, port = proxy.rsplit(":", 1)
        port = int(port)
    except (ValueError, AttributeError):
        return False
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host in ("0.0.0.0", "::", "", "*"):
        host = "127.0.0.1"
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


class Tun2socksManager:
    """Download/verify/spawn/kill the tun2socks bridge for VPN share.

    Idempotent and reconciles toward the DESIRED state each
    :meth:`reconcile` call (running while active + no real tunnel, stopped
    otherwise). Injectable ``run_command`` / ``download`` / ``spawn`` keep
    the tests root-free and off the network.
    """

    def __init__(self, cfg: object, *,
                 run_command: Callable[[list[str]], tuple[int, str]] | None = None,
                 download: Callable[[str, str, Path], tuple[bool, str]] | None = None,
                 spawn: Callable[[list[str]], _Child] | None = None,
                 proxy_probe: Callable[[str], bool] | None = None,
                 verify_delay: float = 0.5,
                 clock: Callable[[], float] | None = None) -> None:
        self._run = run_command or _default_run_command
        self._download = download or _default_download
        self._spawn = spawn or _default_spawn
        self._probe = proxy_probe or _default_proxy_probe
        self._verify_delay = verify_delay
        self._clock = clock or time.monotonic
        vs = getattr(cfg, "vpn_share", None)
        self.interface = (getattr(vs, "tun_interface", "") or "").strip() or "tun0"
        self.binary = (getattr(vs, "binary", "") or "").strip() \
            or "/usr/local/bin/tun2socks"
        self.tun_ip = (getattr(vs, "tun_ip", "") or "").strip() or "10.0.0.1"
        self.tun_gw = (getattr(vs, "tun_gw", "") or "").strip() or "10.0.0.2"
        #: fallback SOCKS proxy. The Config dataclass defaults it to
        #: ``127.0.0.1:10808``; an explicit empty string means "no fallback",
        #: so a missing VPN listener is reported as no-proxy instead of
        #: targeting a made-up port.
        self.socks_fallback = (getattr(vs, "socks_proxy", "") or "").strip()
        self.download_url = (getattr(vs, "download_url", "") or "").strip()
        self.download_sha256 = (getattr(vs, "download_sha256", "") or "").strip()
        self._arch_suffix = ARCH_SUFFIX.get(platform.machine().lower(), "")
        self._child: _Child | None = None
        self._last_spawn = 0.0
        self._spawn_attempted = False
        self._last_download_attempt = 0.0
        self._download_attempted = False
        self._binary_ok = False
        self._status = Tun2socksStatus()

    # ------------------------------------------------------------------
    # internal pieces
    # ------------------------------------------------------------------
    def _binary_path(self) -> Path:
        return Path(self.binary)

    def _find_proxy(self) -> str:
        """Find the VPN client's LOCAL SOCKS listener via ``ss -tlnp``."""
        code, out = self._run(["ss", "-tlnp"])
        if code != 0:
            return ""
        for line in (out or "").splitlines():
            fields = line.split()
            if len(fields) < 6 or not _PROXY_RE.search(" ".join(fields[5:])):
                continue
            local = fields[3]
            if local.count(":") != 1:
                continue  # not a plain host:port
            host = local.rsplit(":", 1)[0]
            if host in ("127.0.0.1", "0.0.0.0", "[::]", "::"):
                return local
        return ""

    def _binary_is_ready(self) -> bool:
        if self._binary_ok:
            return True
        p = self._binary_path()
        if not p.is_file():
            return False
        if not os.access(p, os.X_OK):
            return False
        # probe the binary actually runs (a weird exec failure — wrong arch,
        # foreign format — degrades to "not ready" so it gets re-provisioned)
        try:
            code, _ = self._run([str(p), "--version"])
        except Exception:  # noqa: BLE001
            return False
        if code != 0:
            return False
        self._binary_ok = True
        return True

    def _ensure_binary(self) -> bool:
        """Make sure the binary exists; download it (once, gated) if not."""
        if self._binary_is_ready():
            return True
        now = self._clock()
        if (self._download_attempted and
                now - self._last_download_attempt < RETRY_GATE):
            return False
        self._download_attempted = True
        self._last_download_attempt = now
        suffix = self._arch_suffix
        if not suffix:
            self._status = Tun2socksStatus(
                STATE_NO_BINARY,
                f"unsupported architecture {platform.machine().lower()}")
            return False
        url = self.download_url or f"{RELEASE_BASE}{suffix}.zip"
        sha256 = self.download_sha256 or ARCH_SHA256.get(suffix, "")
        if not sha256:
            self._status = Tun2socksStatus(
                STATE_NO_BINARY, f"no pinned checksum for {suffix} — refusing "
                "to install an unverified binary")
            return False
        self._status = Tun2socksStatus(
            STATE_DOWNLOADING, f"installing tun2socks {RELEASE_TAG} (one-time "
            "download)…")
        ok, message = self._download(url, sha256, self._binary_path())
        if not ok:
            self._status = Tun2socksStatus(STATE_NO_BINARY, message)
            return False
        self._binary_ok = True
        log.info("tun2socks: %s", message)
        return True

    def _configure_interface(self) -> None:
        """Best-effort: give the tun a usable address + make sure it's UP.

        tun2socks v2 creates the device but (unlike v1) assigns no address and
        may not have the link up yet the moment we check — the caller is
        expected to configure it. The routing manager refuses to add
        ``default dev <tun>`` on a down link ("Device for nexthop is not up"),
        so we retry ``ip link set up`` until the device actually exists and is
        up, then stamp the configured ``tun_ip`` (both best-effort, non-fatal —
        the bridge still runs).
        """
        iface = self.interface
        deadline = time.monotonic() + 5.0
        up = False
        while time.monotonic() < deadline:
            try:
                rc, _ = self._run(["ip", "link", "set", "dev", iface, "up"])
            except Exception:  # noqa: BLE001
                rc = 1
            if rc == 0:
                up = True
                break
            time.sleep(0.3)
        for _ in range(5):
            try:
                rc, _ = self._run(
                    ["ip", "addr", "add", f"{self.tun_ip}/24", "dev", iface])
            except Exception:  # noqa: BLE001
                rc = 1
            if rc == 0:
                break
            time.sleep(0.3)
        if not up:
            log.warning("tun2socks: could not bring %s up", iface)

    def _start(self, proxy: str) -> None:
        if self._child is not None and self._child.poll() is None:
            return
        if (self._spawn_attempted and
            self._clock() - self._last_spawn < RESPAWN_GATE):
            self._status = Tun2socksStatus(
                STATE_ERROR, "tun2socks just exited — waiting before retry")
            return
        self._spawn_attempted = True
        self._last_spawn = self._clock()
        # tun2socks v2 CLI (v2.7.0): the flags are -device/-proxy/-interface/
        # -tun-post-up etc. — the old v1 -tun-ip/-tun-gw flags are GONE, and an
        # undefined flag makes Go's `flag` package print an error + os.Exit(2)
        # (the process "just exits"). v2 creates the TUN and brings the link UP
        # itself; we assign the tunnel address separately in _configure_interface.
        argv = [self.binary, "-device", self.interface, "-proxy",
                f"socks5://{proxy}"]
        log.info("tun2socks: spawning %s", " ".join(argv))
        try:
            self._child = self._spawn(argv)
        except Exception as exc:  # noqa: BLE001
            self._child = None
            self._status = Tun2socksStatus(STATE_ERROR, f"spawn failed: {exc}")
            return
        if self._verify_delay:
            time.sleep(self._verify_delay)
        if self._child is None or self._child.poll() is not None:
            self._status = Tun2socksStatus(
                STATE_ERROR, "tun2socks exited — check the VPN client's proxy")
            return
        self._configure_interface()
        self._status = Tun2socksStatus(
            STATE_RUNNING,
            f"sharing the VPN client through {self.interface}", proxy, self.interface)

    def _stop(self) -> None:
        if self._child is not None:
            self._child.stop()
            self._child = None
        self._status = Tun2socksStatus()

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------
    def reconcile(self, active: bool) -> Tun2socksStatus:
        """Bring the bridge to the DESIRED state.

        ``active`` (VPN share ON + the kernel still lacks a tunnel) keeps a
        tun2socks child running; ``False`` stops it. Returns the status for
        the dashboard / API.
        """
        if not active:
            self._stop()
            return Tun2socksStatus()
        proxy = self._find_proxy()
        if not proxy:
            proxy = self.socks_fallback
            # The configured fallback may be a default port nothing listens
            # on (v2rayN without a SOCKS inbound). Spawning into it would
            # blackhole the whole client subnet — report honestly instead.
            if proxy and not self._probe(proxy):
                self._status = Tun2socksStatus(
                    STATE_NO_PROXY,
                    f"no VPN SOCKS proxy listening on {proxy} — enable the "
                    "SOCKS inbound in the VPN client (e.g. v2rayN Settings "
                    "-> Inbound, port 10808)")
                return self._status
        if not proxy:
            self._status = Tun2socksStatus(
                STATE_NO_PROXY, "no VPN SOCKS proxy found — start the VPN "
                "client first")
            return self._status
        if not self._ensure_binary():
            return self._status
        self._start(proxy)
        return self._status