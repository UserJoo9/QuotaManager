"""Passive WiFi/LAN access probe — learns which SSID a device is on (Linux).

The router bridges clients L2, so the gateway box's own network stack only
ever sees every client arrive on the same uplink NIC — the box can never tell
a WiFi client from a wired one from its own packets. But its WiFi card CAN
hear the air directly: even with encryption, 802.11 frames carry the station
MAC and the BSSID in the clear. So this module puts a spare WiFi NIC into
monitor mode and runs ``airodump-ng`` (a Kali/Debian staple) against it; the
CSV it continuously rewrites lists every heard station with the BSSID it is
associated with, plus an AP table mapping BSSIDs to ESSIDs.

run.py combines the probe's ``(mac -> ssid)`` view with the lease table:

* a leased MAC associated with a known BSSID -> "WiFi · <ESSID>"
* a leased MAC heard on the air (probing/associated) -> "WiFi"
* a leased MAC NEVER heard for a grace period -> "LAN" (wired)

The probe runs on a DEDICATED thread (subprocess + CSV I/O never touch the
event loop — the same pattern as quota/dnslog.py). ``airodump-ng`` hops
channels on its own, so a full sweep takes tens of seconds; the tag settles
within a minute or two and then stays live. Missing tools, a non-monitor
card, or non-Linux degrade to ``available=False`` — the dashboard simply
shows no WiFi/LAN tag (or the manual override).

Why airodump-ng and not raw tcpdump: the tool already does channel hopping,
BSSID/ESSID extraction and the station list for us, and ships with Kali
(``aircrack-ng``). One long-lived subprocess writes one small CSV — no frame
parsing in Python at all.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from typing import Callable

log = logging.getLogger("quota.wifi_probe")

#: airodump writes "<write-path>-01.csv" (its single-target numbering).
CSV_SUFFIX = "-01.csv"
#: a station sighting stays "wireless" for this long after its last frame —
#: an associated-but-idle device does not flip to LAN the moment it goes quiet.
SIGHTED_TTL = 600.0
#: CSV cap — airodump files are a few KB; never slurp a runaway file.
CSV_MAX_BYTES = 512 * 1024
#: how often the thread re-reads the CSV (seconds)
POLL_INTERVAL = 5.0

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _is_mac(value: str) -> bool:
    return bool(_MAC_RE.match(value.lower()))


def parse_airodump_csv(text: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Parse airodump-ng's ``--output-format csv`` file.

    Returns ``(bssid -> essid, [(station_mac, bssid_or_empty)])``. The file
    has two sections separated by a blank line: APs (BSSID, ..., ESSID at
    column 13) then stations (Station MAC, ..., BSSID at column 5, Probed
    ESSIDs). A station with no association shows ``(not associated)`` there —
    still a WIRELESS sighting, just without an SSID. Hidden SSIDs (empty or
    ``<length: 0>``) never enter the map. Malformed lines are skipped; a
    non-MAC first column marks a header/garbage row.
    """
    aps: dict[str, str] = {}
    stations: list[tuple[str, str]] = []
    section = "ap"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            section = "station"  # the blank line separates APs from stations
            continue
        cols = [c.strip() for c in line.split(",")]
        if section == "ap" and len(cols) >= 14 and _is_mac(cols[0]):
            essid = cols[13].strip()
            if essid and not essid.startswith("<length:"):
                aps[cols[0].lower()] = essid
        elif section == "station" and len(cols) >= 6 and _is_mac(cols[0]):
            bssid = cols[5].strip().lower()
            stations.append((cols[0].lower(),
                             bssid if _is_mac(bssid) else ""))
    return aps, stations


class WifiProbe:
    """Monitor-mode passive WiFi/LAN classifier (dedicated thread).

    Construction is cheap and opens nothing; :meth:`start` runs the thread,
    which brings up monitor mode (``airmon-ng`` with a direct ``iw`` fallback)
    and spawns airodump-ng writing into ``csv_base-01.csv``. The thread then
    re-reads that CSV every ``poll_interval`` and keeps a sighting table.
    :meth:`snapshot` returns the thread-safe view run.py consumes.

    ``auto_start=False`` (tests) skips the airmon-ng/airodump subprocesses —
    the caller feeds the CSV path and drives :meth:`_read_csv` directly.
    """

    def __init__(self, interface: str = "", csv_base: str = "/tmp/quota-wifi",
                 poll_interval: float = POLL_INTERVAL,
                 sighted_ttl: float = SIGHTED_TTL,
                 cmd_runner: Callable[[list[str]], tuple[int, str]] | None = None,
                 popen_factory: Callable[[list[str]], object] | None = None,
                 auto_start: bool = True) -> None:
        self.interface = interface
        self.csv_path = f"{csv_base}{CSV_SUFFIX}"
        self._poll_interval = poll_interval
        self._sighted_ttl = sighted_ttl
        #: injectable `airmon-ng`/`iw` runner (tests fake it)
        self._cmd = cmd_runner or self._default_run
        #: injectable process spawner for the long-lived airodump (tests use
        #: auto_start=False instead; the default is a real Popen).
        self._popen = popen_factory or subprocess.Popen
        self._auto_start = auto_start
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._mon_iface = ""
        self._lock = threading.Lock()
        #: probe state (written by the thread, read by snapshot())
        self._available = False
        self._error = ""
        self._ssid_by_mac: dict[str, str] = {}
        self._sighted: dict[str, float] = {}
        self._ssids: list[str] = []
        self.running = False

    @staticmethod
    def _default_run(argv: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=10)
            return proc.returncode, proc.stdout
        except Exception:  # noqa: BLE001  (missing binary / timeout -> ""
            return 1, ""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="wifi-probe",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001  (already dead)
                pass
            self._proc = None
        if self._mon_iface:
            self._cmd(["airmon-ng", "stop", self._mon_iface])

    # -- capture setup (real hardware only) --------------------------------

    def _start_capture(self) -> None:
        iface = self.interface
        if not iface:
            code, out = self._cmd(["iw", "dev"])
            if code == 0:
                for m in re.finditer(r"Interface\s+(\S+)", out or ""):
                    if m.group(1).startswith("wlan"):
                        iface = m.group(1)
                        break
            if not iface:
                iface = "wlan0"
        code, _ = self._cmd(["airmon-ng", "start", iface])
        if code == 0:
            self._mon_iface = f"{iface}mon"
        else:
            # direct nl80211 monitor (driver without airmon-ng quirks)
            if self._cmd(["iw", "dev", iface, "set", "type", "monitor"])[0] == 0:
                self._mon_iface = iface
            else:
                raise RuntimeError(
                    f"cannot put {iface} into monitor mode "
                    "(no monitor support / rfkill?)")
        self._proc = self._popen([
            "airodump-ng", "--band", "abg", "--write",
            self.csv_path[:-len(CSV_SUFFIX)], "--write-interval", "5",
            "--output-format", "csv", self._mon_iface])
        log.info("wifi probe: monitoring %s (airodump pid %s)",
                 self._mon_iface, getattr(self._proc, "pid", "?"))

    # -- main loop ---------------------------------------------------------

    def _loop(self) -> None:
        self.running = True
        try:
            if self._auto_start:
                try:
                    self._start_capture()
                except Exception as exc:  # noqa: BLE001  -> degrade gracefully
                    with self._lock:
                        self._error = str(exc)
                    self._stop.wait(self._poll_interval)
            while not self._stop.is_set():
                try:
                    self._read_csv()
                except Exception as exc:  # noqa: BLE001
                    log.warning("wifi probe: CSV read failed: %s", exc)
                self._stop.wait(self._poll_interval)
        finally:
            self.running = False

    def _read_csv(self) -> None:
        """One CSV pass: parse APs + stations, refresh the sighting table."""
        try:
            size = os.path.getsize(self.csv_path)
            if size <= 0:
                return
            with open(self.csv_path, "rb") as fh:
                if size > CSV_MAX_BYTES:
                    fh.seek(size - CSV_MAX_BYTES)
                text = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return  # airodump has not written yet — keep polling
        aps, stations = parse_airodump_csv(text)
        now = time.time()
        sighted: dict[str, float] = {}
        ssid_by_mac: dict[str, str] = {}
        for mac, bssid in stations:
            sighted[mac] = max(sighted.get(mac, 0.0), now)
            if bssid and bssid in aps:
                ssid_by_mac[mac] = aps[bssid]
        with self._lock:
            self._ssid_by_mac = ssid_by_mac
            for mac, ts in sighted.items():
                self._sighted[mac] = max(self._sighted.get(mac, 0.0), ts)
            cutoff = now - self._sighted_ttl
            for mac in [m for m, ts in self._sighted.items() if ts < cutoff]:
                del self._sighted[mac]
            self._ssids = sorted({s for s in aps.values() if s})
            self._available = True
            self._error = ""

    # -- consumption --------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """Thread-safe view for run.py / the API: the per-MAC SSID map, the
        wireless sighting set, and the detected ESSID list (for the manual
        override picker). ``available=False`` => no radio data (the UI falls
        back to the box-NIC tag / manual override)."""
        with self._lock:
            return {
                "available": self._available,
                "error": self._error,
                "ssid_by_mac": dict(self._ssid_by_mac),
                "wireless_macs": sorted(self._sighted.keys()),
                "ssids": list(self._ssids),
            }