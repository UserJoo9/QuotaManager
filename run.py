"""Quota Manager entrypoint.

Wires every layer together in one process on the Linux gateway (Kali/Debian).
The kernel owns the network path: dnsmasq serves DHCP + DNS on a dedicated
client subnet (192.168.2.0/24, gateway = this box) which the kernel
masquerades out the uplink, and the nftables engine (``quota/nftables.py``)
counts + drops at line rate.

Always:
* Non-blocking logging (QueueHandler -> writer thread -> rotating file).
* SQLite (aiosqlite), quota service, per-period allowance snapshot.
* A maintenance coroutine that flushes engine counters to SQLite every 15 s,
  re-evaluates block states, and pushes fresh enforcement maps to the engine.
* FastAPI/uvicorn (REST + WebSocket push + static glassmorphism UI).

Any sub-component that cannot start (no ``nft``, no root, no dnsmasq) degrades
gracefully: the rest keeps running and the dashboard still reports usage that
has been flushed so far. This file never crashes the whole app on an optional
subsystem failure.

Usage
-----
    python run.py
    python run.py --config config.yaml --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import uvicorn

from api.app import create_app
from core import config as cfg_mod
from core.logging_setup import setup_logging
from quota import db as _db
from quota import dns_rules as _dns_rules
from quota.arp_scan import ArpScanner
from quota.dnslog import DnslogTailer
from quota.engine import GATEWAY_MAC, EngineSnapshot, SnapshotHolder
from quota.firewall import FirewallManager
from quota.latency_probe import ArpRttProbe
from quota.netmgr import TopologyManager
from quota.nftables import NftablesEngine
from quota.service import QuotaService
from quota.topology import (check_internet, check_internet_dns, detect_ppp,
                            restart_pppoe)
from quota.updater import Updater

log = logging.getLogger("quota.run")


#: VPN-client process names the auto-learn step matches against ``ss`` output.
_VPN_CLIENT_RE = re.compile(r"v2ray|sing[-_ ]?box|xray|tun2socks",
                            re.IGNORECASE)


def _default_run_command(argv: list[str]) -> tuple[int, str]:
    import subprocess
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return proc.returncode, proc.stdout
    except Exception:  # noqa: BLE001  (subprocess + timeout failures -> "" )
        return 1, ""


def _iter_json_neigh(out: str):
    """Yield parsed rows of ``ip -j neigh`` JSON, tolerating junk.

    iproute2 emits a JSON array of objects ({dst, dev, lladdr, state, ...}).
    Older versions may interleave plain-text lines — each is skipped. A
    corrupted/partial payload degrades to no rows (never raises).
    """
    import json as _json

    text = (out or "").strip()
    if not text:
        return
    start = text.find("[")
    if start < 0:
        return
    try:
        rows = _json.loads(text[start:])
    except ValueError:
        return
    if not isinstance(rows, list):
        return
    for row in rows:
        if isinstance(row, dict):
            yield row


def _learn_vpn_servers(
        run_command: Callable[[list[str]], tuple[int, str]] | None = None,
) -> set[str]:
    """IPv4 peers of the box's VPN client process, via ``ss``.

    The "VPN share" relay rides the box's own connection(s) to the VPN
    server(s) (clients -> tun -> VPN client on the box -> server). Those
    server IPs must stay reachable when the box's OWN internet is cut
    (``gw_blocked``) or the tunnel dies — the maintenance loop feeds the
    learned set into the engine's ``gw_allowed`` whitelist. Matches the
    ``ss`` Process column against the VPN-client process names; IPv6 peers
    are skipped (the engine's ``gw_allowed`` set is ``ipv4_addr``). Any
    subprocess failure degrades to an empty set (never raises).
    """
    run = run_command or _default_run_command
    peers: set[str] = set()
    for argv in (["ss", "-tnp"], ["ss", "-unp"]):
        code, out = run(argv)
        if code != 0:
            continue
        for line in (out or "").splitlines():
            fields = line.split()
            if len(fields) < 6 or not _VPN_CLIENT_RE.search(
                    " ".join(fields[5:])):
                continue
            peer = fields[4]
            # State Recv-Q Send-Q Local:Port Peer:Port Process...
            if peer.count(":") != 1 or peer == "*:*":
                continue  # skip IPv6 / wildcard peer rows
            peers.add(peer.rsplit(":", 1)[0])
    return peers


def _make_engine(cfg: cfg_mod.Config, holder) -> NftablesEngine:
    """Build the accounting/block engine (always nftables on Linux).

    ``engine.backend`` is accepted for config compatibility but the gateway is
    Linux-only, so the kernel nftables engine is the only implementation.
    """
    return NftablesEngine(cfg, holder)


def _make_shaper(cfg: cfg_mod.Config):
    """Build the tc shaper, or None when shaping is off/unsupported."""
    from quota.shaping import TcShaper  # lazy: needs tc + ifb + root

    shaping_cfg = getattr(cfg, "shaping", None)
    if shaping_cfg is not None and not shaping_cfg.enabled:
        log.warning("speed shaping disabled in config — per-device / per-user "
                    "speed limits + low-latency queues off")
        return None
    return TcShaper(cfg)


def _make_dns_manager(cfg: cfg_mod.Config) -> _dns_rules.DnsRuleManager | None:
    """Build the DNS-filtering config writer, or None when disabled."""
    dns_cfg = getattr(cfg, "dns_filter", None)
    if dns_cfg is not None and not dns_cfg.enabled:
        log.warning("DNS filtering disabled in config — domain rules, "
                    "presets, and per-client DNS-server overrides are inert")
        return None
    if dns_cfg is None:
        return _dns_rules.DnsRuleManager()
    return _dns_rules.DnsRuleManager(
        conf_dir=dns_cfg.conf_dir, tags_file=dns_cfg.tags_file,
        rules_file=dns_cfg.rules_file, reload_dnsmasq=dns_cfg.reload_dnsmasq)


def _make_vpn_manager(cfg: cfg_mod.Config):
    """Build the VPN-share policy-routing manager, or None when disabled."""
    from quota.vpnshare import VpnShareManager  # lazy: sysfs + `ip`

    vs_cfg = getattr(cfg, "vpn_share", None)
    if vs_cfg is not None and not vs_cfg.enabled:
        log.warning("VPN share disabled in config — client subnet is never "
                    "routed into the box's tunnel")
        return None
    return VpnShareManager(cfg)


def _reload_dnsmasq() -> None:
    """Restart dnsmasq so it picks up a changed DHCP-refusal fragment.

    Mirrors DnsRuleManager._reload: ``dnsmasq --test`` validates the whole
    effective config first (a bad fragment must never take DHCP + DNS down
    for the household), then ``systemctl restart dnsmasq`` re-reads the
    conf-dir files. Failures are logged, never raised.
    """
    try:
        probe = subprocess.run(
            ["dnsmasq", "--test"], capture_output=True, text=True, timeout=10)
        if probe.returncode != 0:
            log.error("generated dnsmasq config failed validation — NOT "
                      "restarting: %s", probe.stderr.strip())
            return
    except FileNotFoundError:
        log.warning("`dnsmasq` binary not found — skipping the pre-restart "
                    "validation check (restarting anyway)")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not run `dnsmasq --test` (%s) — restarting anyway", exc)
    try:
        subprocess.run(["systemctl", "restart", "dnsmasq"],
                       capture_output=True, text=True, timeout=15, check=True)
        log.info("dnsmasq restarted to apply DHCP refusal changes")
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("failed to restart dnsmasq after a DHCP refusal change: %s", exc)


def _make_tun2socks_manager(cfg: cfg_mod.Config):
    """Build the tun2socks auto-provisioner, or None when disabled.

    ``vpn_share.tun2socks: false`` skips it entirely (users running their
    own kernel-TUN client — sing-box/xray/WireGuard — don't need a bridge,
    and a second tun would confuse VpnShareManager's tunnel detector).
    """
    from quota.tun2socks import Tun2socksManager  # lazy: platform + urllib

    vs_cfg = getattr(cfg, "vpn_share", None)
    if vs_cfg is None or not vs_cfg.enabled or not vs_cfg.tun2socks:
        return None
    return Tun2socksManager(cfg)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Quota Manager gateway")
    p.add_argument("--config", default=None, help="path to config.yaml")
    env_port = os.environ.get("QUOTA_PORT")
    p.add_argument(
        "--port",
        type=int,
        default=int(env_port) if env_port and env_port.isdigit() else None,
        help="override web port (can also set QUOTA_PORT env var)",
    )
    p.add_argument("--debug", action="store_true", help="enable debug logging")
    return p.parse_args()


class Gateway:
    """Owns all long-lived objects so tests can construct the wiring once."""

    #: rogue LAN scan cadence — slower than the 15 s tick on purpose (a raw
    #: ARP probe of both /24s costs a few hundred frames; every 60 s is plenty).
    ROGUE_SCAN_INTERVAL = 60.0
    #: DNS-history TTL prune cadence — once an hour is plenty (the buckets are
    #: minute-granular; a whole day of rows is a few thousand, not millions).
    DNS_PRUNE_INTERVAL = 3600.0
    #: audit-trail (events table) prune cadence + retention. The table is
    #: append-only and otherwise unbounded — the only real disk-growth risk.
    EVENTS_PRUNE_INTERVAL = 3600.0
    EVENTS_RETENTION_SEC = 30 * 86400

    def __init__(self, cfg: cfg_mod.Config,
                 config_path: str | Path | None = None,
                 internet_probe: Callable[[], bool] | None = None,
                 dns_probe: Callable[[], bool] | None = None) -> None:
        self.cfg = cfg
        # The on-disk config.yaml the app loaded — the runtime topology apply
        # (WAN tab) patches this file, so we must know exactly which one it is.
        self.config_path: Path | None = Path(config_path) if config_path else None
        #: Internet-reachability probe for the WAN tab's green dot (see
        #: quota.topology.check_internet). Injectable so tests fake the network.
        self.internet_probe = internet_probe or check_internet
        #: DNS-reachability probe used INSTEAD of ``internet_probe`` while the
        #: box's own internet is deliberately cut (``gw_blocked`` drops the TCP
        #: probe, but UDP 53 is exempted — see quota.topology.check_internet_dns).
        self.dns_probe = dns_probe or check_internet_dns
        self.database = _db.Database(cfg.db_path)
        self.service = QuotaService(self.database, timezone=cfg.timezone)
        self.holder = SnapshotHolder()
        self.engine: NftablesEngine | None = None
        self.shaper: object | None = None  # quota.shaping.TcShaper (tc/ifb)
        self.dns_manager: _dns_rules.DnsRuleManager | None = None  # domain filtering
        #: VPN-share policy routing (quota.vpnshare.VpnShareManager); None
        #: when cfg.vpn_share.enabled is false. Reconciles the client-subnet
        #: routing into the box's tunnel + the engine's gateway-meter
        #: suspension on every tick (and right after a Network-tab toggle).
        self.vpn_manager: object | None = None
        #: tun2socks auto-provisioner (quota.tun2socks.Tun2socksManager);
        #: None when cfg.vpn_share.tun2socks is false. Bridges userspace
        #: VPN clients (v2rayN — no kernel tun) by downloading the pinned
        #: binary on first use and spawning it against the client's SOCKS.
        self.tun2socks_manager: object | None = None
        #: last VPN-share kernel status, surfaced through /api/network's
        #: ``vpn_share.status`` key (no DB/subprocess reads on the request
        #: path — the tick owns the probing).
        self._last_vpn_status: dict[str, object] = {"state": "off"}
        #: STICKY auto-learned VPN-server IPs. While relaying, the box must
        #: keep reaching its VPN server(s) even under a Gateway cut; a learned
        #: set is only ADDED to (never shrunk) while the relay is on, so the
        #: VPN client can re-dial without a 15 s window where the server is
        #: unreachable. Cleared when the relay turns off (the cut then blocks
        #: the box entirely again). Feed into engine.set_gateway_allowed().
        self._vpn_allowed: set[str] = set()
        #: injectable learner (tests fake the `ss` probe); default _learn_vpn_servers
        self._vpn_learn: Callable[..., set[str]] = _learn_vpn_servers
        self.arp_lock: object | None = None  # quota.arp_lock.ArpLock (opt-in)
        #: Kernel firewall (quota.firewall.FirewallManager — the separate
        #: ``inet quota_firewall`` nftables table layered BEFORE the quota
        #: engine, forward/input priority -100). Built in startup() AFTER the
        #: topology override so its LAN/WAN posture derives from the effective
        #: topology. None when cfg.firewall.enabled is false.
        self.firewall: FirewallManager | None = None
        # Built in startup(), AFTER the DB topology override: the scanner
        # resolves its probe networks from cfg at construction, so building it
        # here (before the override exists) would probe the wrong subnets in
        # WAN mode. A test may inject a fake before startup() (None-guard).
        self.arp_scanner: ArpScanner | None = None
        #: Runtime LAN/WAN switch (dashboard WAN tab); built in startup().
        self.topology_manager: TopologyManager | None = None
        #: DNS-history log tailer (quota.dnslog); built in startup() when
        #: cfg.history.enabled. A dedicated thread tails dnsmasq's query log
        #: so file I/O never touches the event loop.
        self.dnslog: DnslogTailer | None = None
        #: GitHub self-update manager (quota.updater): 24 h release checks, the
        #: Admin-tab update card, and the optional auto-install. Owned here so
        #: the maintenance tick drives the cadence and the app reads its state.
        #: ``cfg.updates.enabled`` is the master switch (hermetic tests + a
        #: deployment opt-out); None => the endpoints 404 and the snapshot
        #: carries ``update: None`` (degraded boot).
        self.updater: Updater | None = (
            Updater(self.database,
                    repo=cfg.updates.repo,
                    interval_sec=cfg.updates.interval_hours * 3600)
            if cfg.updates.enabled else None)
        #: last DNS-history TTL prune's clock, for the hourly gate
        self._last_dns_prune = time.monotonic()
        #: last rogue scan's result, surfaced through the holder every tick
        self._rogues: list[object] = []
        #: start the scan clock NOW, not at boot: the first scan fires 60 s
        #: after startup (leases have settled), never during the boot tick.
        self._last_rogue_scan = time.monotonic()
        self._known_rogue_macs: set[str] = set()
        #: the live ip->mac map from the last maintenance tick. A speed-limit
        #: edit re-syncs the shaper immediately (no 15 s wait), and that re-sync
        #: needs the same map — rebuilt fresh every tick, kept here for the API
        #: callback (_reshaping_now) to reuse between ticks.
        self._last_ip_to_mac: dict[str, str] = {}
        #: serializes _sync_shaping (the maintenance tick + an API-triggered
        #: immediate re-sync). _sync_shaping reads the DB before it programs tc,
        #: so without a lock a tick that read the caps BEFORE an edit committed
        #: could re-apply its stale snapshot AFTER the immediate re-sync and
        #: briefly undo the user's fresh caps. Whoever runs second re-reads the
        #: DB, so the lock makes both orderings end on the fresh state.
        self._shaping_lock = asyncio.Lock()
        #: serializes DNS-rule regeneration the same way ``_shaping_lock``
        #: does for the tc tree (a maintenance tick and an API-triggered
        #: immediate apply must not race each other's DB read + file write).
        self._dns_lock = asyncio.Lock()
        #: serializes VPN-share reconciliation (tick + API immediate apply)
        #: the same way ``_shaping_lock`` serializes the tc tree.
        self._vpn_lock = asyncio.Lock()
        #: serializes WAN public-IP renewals (manual Restart button + the
        #: auto-renew schedule). Renewal restarts the PPPoE dial — two renews
        #: at once would tear the session down twice (``_renew_wan_ip`` is the
        #: only caller and it is already serialized per-tick; the lock also
        #: guards a manual click landing mid-tick).
        self._renew_lock = asyncio.Lock()
        #: the last PPPoE restart result ({restarted, state, detail}); logged
        #: and exposed to tests (no API surface of its own — the auto-renew
        #: timestamp in the DB is what the WAN tab shows).
        self._last_wan_renew: dict[str, object] | None = None
        #: injectable PPPoE dial restarter (tests fake the systemctl calls);
        #: default quota.topology.restart_pppoe.
        self._pppoe_restart: Callable[..., dict[str, object]] = restart_pppoe
        self._maintenance_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        #: last audit-trail (events table) prune's clock, for the hourly gate
        self._last_events_prune = time.monotonic()
        #: last-applied tc state, mirrored out of the shaper for the Network
        #: panel's "applying…" indicator (shaper.applied is a property — read
        #: it once per tick, off the tc call itself). Defaults assume applied
        #: so a no-shaper boot (shaping off) never shows a stale warning.
        self._shaping_applied = True
        self._shaping_available = False
        #: injectable `ip` runner for the source-interface (neighbor table)
        #: collector — tests fake it; defaults to the module runner.
        self._ip_run: Callable[[list[str]], tuple[int, str]] = _default_run_command
        #: Passive WiFi probe (quota/wifi_probe.py); built in startup()
        #: when cfg.network.wifi_probe.enabled. A dedicated thread hears the
        #: air in monitor mode; the snapshot drives the yield check in
        #: _maybe_latency_tick (when the monitor probe is available, the
        #: ARP-RTT probe yields).
        self.wifi_probe: object | None = None
        #: probe snapshot mirrored for the API (availability check).
        self._wifi_probe_state: dict[str, object] = {
            "available": False, "ssids": [], "error": ""}
        #: ARP-RTT probe by ARP round-trip time (quota/latency_probe.py).
        #: Works on ANY hardware — no monitor-mode card required; it is the
        #: fallback the monitor probe hands to when the box's WiFi module can't
        #: sniff the air. Built in __init__ (no sockets opened); tests inject
        #: a fake. None when disabled in config.
        self.latency_probe: object | None = None
        lcfg = getattr(cfg.network, "latency_probe", None)
        if lcfg is not None and getattr(lcfg, "enabled", True):
            self.latency_probe = ArpRttProbe(
                cfg,
                samples=int(getattr(lcfg, "samples", 6) or 6),
                timeout_s=float(getattr(lcfg, "timeout_s", 0.5) or 0.5))
            # explicit None check — `x or 30.0` would swallow a legitimate 0
            # (sweep every tick, used by tests)
            _l_iv = getattr(lcfg, "interval_s", None)
            self._latency_interval = (
                float(_l_iv) if _l_iv is not None else 30.0)
        else:
            self._latency_interval = 30.0
        self._last_latency_sweep = time.monotonic()
        #: IPs that answered the last ARP sweep — the "connected NOW" source
        #: for the LED (a lease alone lags reality by up to LEASE_HOURS).
        self._latency_responders: set[str] = set()
        self._latency_sweep_ran = False
        #: IPs with an active kernel neighbor entry (REACHABLE/STALE/DELAY).
        #: Reinforces the ARP probe: a device that missed the probe burst but
        #: is generating traffic still has a valid ARP mapping in the kernel.
        self._neigh_active_ips: set[str] = set()
        #: set when _persist_lease creates a new device — _sync_dnsmasq_leases
        #: resets _last_latency_sweep so the ARP probe runs in the same tick,
        #: turning the new device's LED blue immediately instead of waiting up
        #: to a full sweep interval (30 s).
        self._new_devices_detected = False

    # ------------------------------------------------------------- startup

    async def startup(self) -> None:
        """Connect DB, ensure period, start optional subsystems."""
        await self.database.connect()
        # A dashboard WAN-toggle applies on the NEXT restart: the DB override
        # must land on cfg BEFORE the engine/scanner are built below (they read
        # engine.topology + the resolved local subnets at construction).
        await self._apply_topology_override()
        # The runtime LAN/WAN switch (dashboard WAN tab): patches config.yaml +
        # the DB together, runs scripts/topology.sh, schedules a detached
        # restart. Built AFTER the override so its LAN snapshot reads the final
        # cfg (the override can flip engine.topology from the DB).
        self.topology_manager = TopologyManager(
            self.cfg, self.database,
            config_path=self.config_path,
            script_path=cfg_mod.PROJECT_ROOT / "scripts" / "topology.sh")
        await self._seed_bundle_from_cfg()
        await self.service.ensure_period()
        log.info("database ready: %s", self.cfg.db_path)

        # -- startup self-heal (infrastructure resilience) -----------------
        # The setup script creates ``quota_nat`` (masquerade) and the NIC
        # addresses once; either can be silently lost (nft flush ruleset,
        # NM reconnection, broken symlink).  Recreate anything missing
        # before the engine / firewall depend on it.  Best-effort — a
        # failure is logged and never prevents the app from starting.
        self._startup_self_heal()

        # -- packet engine (nftables kernel rules) --------------------------
        if self.cfg.engine.enabled:
            self.engine = _make_engine(self.cfg, self.holder)
            self.engine.start()
            log.info("packet engine started (%s)", getattr(self.engine, "name", "?"))
        else:
            log.warning("packet engine disabled in config — no per-packet accounting")

        # -- speed shaping (tc) ---------------------------------------------
        # Kernel-side HTB + fq_codel per-device / per-user speed limits. The
        # maintenance loop feeds it settings + the live IP map each tick; the
        # shaper only rebuilds the kernel tree when something changed.
        self.shaper = _make_shaper(self.cfg)
        if self.shaper is not None:
            self.shaper.start()

        # -- DNS filtering (dnsmasq domain rules + per-client DNS servers) --
        # Pure config-generation, no kernel/socket state to "start" — built
        # here (not __init__) for symmetry with the shaper/dnslog and so a
        # future config-driven rebuild has the same lifecycle shape. An
        # initial apply happens after the maintenance loop's first tick
        # (device tags need a device list), not here — see _maintenance_tick.
        self.dns_manager = _make_dns_manager(self.cfg)

        # -- VPN share (policy routing into the box's tunnel) ----------------
        # Reconcile once at boot (the DB switch may say on — the rules must
        # land before any client traffic; a crashed previous run's leftovers
        # are removed when the switch is off — see VpnShareManager.reconcile),
        # then every maintenance tick + right after a Network-tab toggle.
        self.vpn_manager = _make_vpn_manager(self.cfg)
        self.tun2socks_manager = _make_tun2socks_manager(self.cfg)
        await self._sync_vpn_share()

        # -- kernel firewall (nftables quota_firewall) ------------------------
        # A SEPARATE table layered BEFORE the quota engine (forward/input hook
        # priority -100, the engine is 0): firewall-denied traffic never reaches
        # the quota counters, and quota cuts still apply afterward. It never
        # touches quota_gateway / quota_nat / quota_arp_lock. Built AFTER the
        # topology override so its LAN/WAN posture derives from the EFFECTIVE
        # topology. Reconciles on every maintenance tick; the API drives
        # safe-apply (watchdog auto-revert), bans, and the WAN-transition
        # pre-apply (no exposure window).
        if getattr(self.cfg, "firewall", None) is not None \
                and self.cfg.firewall.enabled:
            self.firewall = FirewallManager(self.cfg, self.database,
                                            web_port=self.cfg.web.port)
            await self.firewall.load_config()
            await self.firewall.load_geo()
            await asyncio.to_thread(self.firewall.apply)
            log.info("firewall started (%s mode)",
                     "wan" if self.firewall.topology == "wan" else "lan")

        # -- ARP gateway-lock (opt-in) ---------------------------------------
        # Deny internet to devices that bypass the box by using the ROUTER as
        # their gateway (static-IP cheat). The engine already programmed the
        # deny rules (quota/nftables.py, engine.gateway_arp_lock); this starts
        # the continuous responder that makes the bypasser resolve the router's
        # IP to the box's MAC so its frames arrive at the box to be dropped.
        # Skipped in WAN mode — the box terminates the line itself, so there is
        # no router on the client segment to lock against (no-op anyway, but
        # don't even start the raw-socket thread).
        if (getattr(self.cfg.engine, "gateway_arp_lock", False)
                and getattr(self.cfg.engine, "topology", "lan") != "wan"):
            from quota.arp_lock import ArpLock  # lazy: raw sockets + a thread

            self.arp_lock = ArpLock(self.cfg)
            self.arp_lock.start()

        # -- rogue scanner ----------------------------------------------------
        # Built here (not in __init__) so it resolves its probe networks after
        # the DB topology override above: in WAN mode it probes ONLY the client
        # subnet (no uplink LAN). The None-guard lets a test inject a fake.
        if self.arp_scanner is None:
            self.arp_scanner = ArpScanner(self.cfg)

        # -- DNS browsing history (quota.dnslog) ------------------------------
        # Tail dnsmasq's query log on a dedicated thread and bucket queries
        # into dns_history each tick. Disabled via cfg.history.enabled: false
        # => the tailer is never started (recording ceases entirely).
        if getattr(self.cfg, "history", None) is not None and self.cfg.history.enabled:
            resume: dict[str, object] = {}
            try:
                resume = json.loads(
                    await self.database.get_setting("dnslog_state", "{}") or "{}")
            except ValueError:
                log.warning("dnslog: ignoring unparseable dnslog_state setting")
                resume = {}
            self.dnslog = DnslogTailer(self.cfg.history.dnsmasq_log_file,
                                       resume=resume)
            self.dnslog.start()
            log.info("DNS-history tailer started (%s, resume=%s)",
                     self.cfg.history.dnsmasq_log_file,
                     "yes" if resume else "no")

        # -- passive WiFi probe (quota.wifi_probe) -----------------------
        # A dedicated thread runs airodump-ng against a monitor-mode WiFi card
        # and learns which SSID each device is actually associated with (the
        # router bridges clients L2, so the box's own NICs can't tell). OFF by
        # default — the probe card must be a spare one. A test may inject a
        # fake before startup() (None-guard, same as arp_scanner).
        wprobe_cfg = getattr(getattr(self.cfg, "network", None), "wifi_probe", None)
        if self.wifi_probe is None and wprobe_cfg is not None \
                and getattr(wprobe_cfg, "enabled", False):
            from quota.wifi_probe import WifiProbe

            self.wifi_probe = WifiProbe(
                interface=getattr(wprobe_cfg, "interface", "") or "",
                poll_interval=float(
                    getattr(wprobe_cfg, "poll_interval", 5.0) or 5.0),
                sighted_ttl=float(
                    getattr(wprobe_cfg, "sighted_ttl", 600.0) or 600.0))
        if self.wifi_probe is not None:
            self.wifi_probe.start()
            log.info("WiFi probe started (interface %r)",
                     getattr(wprobe_cfg, "interface", "") or "auto")

        # -- DHCP + DNS -----------------------------------------------------
        # dnsmasq owns these (served on the client subnet by the setup
        # script). We only LEARN device bindings from its lease file (see
        # _sync_dnsmasq_leases).
        log.info("DHCP + DNS via dnsmasq on the client subnet — Python "
                 "DHCP/DNS/ARP stack not used")

        # -- maintenance loop -----------------------------------------------
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        self._install_signal_handlers()

    async def _seed_bundle_from_cfg(self) -> None:
        """Sync the DB bundle from config.yaml unless the dashboard owns it.

        config.yaml is the default source of truth for ``bundle.total_gb`` /
        ``bundle.reset_day``. On every startup those values are re-applied, so
        editing the YAML actually reaches the dashboard (this was the bug:
        the YAML only seeded on the very first boot and the DB was then
        authoritative forever, so a UI change was lost on restart and a YAML
        change never appeared). Once the admin edits the bundle or recharges
        through the dashboard, a ``bundle_source`` setting is set to
        "dashboard" and config.yaml stops being applied — otherwise a restart
        would wipe a recharge or a UI edit.
        """
        if await self.database.get_setting("bundle_source", "config") == "dashboard":
            return
        b = await self.database.get_bundle()
        b.total_gb = self.cfg.bundle.total_gb
        b.reset_day = self.cfg.bundle.reset_day
        b.period_type = self.cfg.bundle.period_type
        await self.database.set_bundle(b)
        # Recompute the allowance snapshot too: an edit to config.yaml's
        # total_gb/reset_day must reach the per-device quotas, otherwise the
        # next evaluate_blocks run compares usage against yesterday's numbers.
        # recompute_allowances preserves period_start, so an existing period
        # and its recorded usage survive the resync.
        await self.service.recompute_allowances()
        log.info("bundle synced from config.yaml: %.1f GB, reset day %d",
                 b.total_gb, b.reset_day)

    def _startup_self_heal(self) -> None:
        """Verify / recreate infrastructure the app depends on but doesn't own.

        The setup script creates ``quota_nat`` (masquerade NAT) and the NIC
        static IPs; either can be silently lost (``nft flush ruleset``,
        NM reconnection, broken ``/etc/nftables.conf`` symlink).  This runs
        once at startup before the engine / firewall are built.
        """
        from quota.startup_health import (  # lazy: subprocess + /proc
            ensure_nat_table, ensure_network_infrastructure)

        # -- NAT table ----------------------------------------------------
        # Derive the client subnet from config (same logic as the engine).
        client_subnet = (getattr(self.cfg.engine, "client_subnet", "") or "").strip()
        if not client_subnet:
            gw = getattr(self.cfg.dhcp, "gateway_ip", "") or ""
            mask = getattr(self.cfg.dhcp, "subnet", "") or ""
            if gw and mask:
                from ipaddress import ip_network
                try:
                    client_subnet = str(ip_network(f"{gw}/{mask}", strict=False))
                except ValueError:
                    pass
        if client_subnet:
            ensure_nat_table(client_subnet)
        else:
            log.warning("startup self-heal: cannot derive client_subnet — "
                        "skipping NAT table check")

        # -- NIC addresses + ip_forward + nftables.conf symlink -----------
        uplink_ip = getattr(self.cfg.dhcp, "uplink_ip", "") or ""
        lan_cidr = getattr(self.cfg.dhcp, "lan_cidr", 24) or 24
        gateway_ip = getattr(self.cfg.dhcp, "gateway_ip", "") or "192.168.2.1"
        ensure_network_infrastructure(
            gateway_ip=gateway_ip,
            uplink_ip=uplink_ip,
            lan_cidr=int(lan_cidr),
        )

    async def _apply_topology_override(self) -> None:
        """Apply a dashboard WAN-mode toggle (takes effect on the NEXT restart).

        Mirrors ``_seed_bundle_from_cfg``: config.yaml seeds ``engine.topology``
        (the setup script writes it), but once the admin toggles the WAN tab a
        ``topology_source=dashboard`` setting is stored and the DB value wins
        from then on — until the admin toggles back (which writes ``lan``).
        Must run BEFORE the engine + rogue scanner are built, because both read
        ``engine.topology`` / the resolved local subnets at construction.
        """
        if await self.database.get_setting("topology_source", "config") != "dashboard":
            return
        db_topology = await self.database.get_setting("topology", "lan")
        if db_topology not in ("lan", "wan"):
            log.warning("invalid topology setting %r — keeping config.yaml", db_topology)
            return
        self.cfg.engine.topology = db_topology
        log.info("topology from dashboard: %s (overrides config.yaml)", db_topology)

    # ------------------------------------------------------------- callbacks

    async def _firewall_wan_preapply(self, target_topology: str) -> None:
        """Program the firewall in the TARGET topology BEFORE the WAN applier.

        The live LAN->WAN switch (dashboard WAN tab) runs ``topology.sh`` which
        dials ppp0 and schedules a restart. Between ppp0 coming up and the
        restart the OLD (LAN-posture) firewall would be in place — an inbound
        exposure window. Re-render + apply in the target posture first; the
        in-memory cfg keeps the current topology until the restart (the
        override is transient).
        """
        if self.firewall is None or not getattr(self.firewall, "available", False):
            return
        self.firewall.set_topology_override(target_topology)
        try:
            await asyncio.to_thread(self.firewall.apply)
        except Exception:  # noqa: BLE001
            log.exception("firewall WAN-preapply failed")
        finally:
            self.firewall.set_topology_override(None)

    async def _persist_lease(self, mac: str, ip: str) -> None:
        try:
            await self.database.upsert_lease(mac, ip, self.cfg.dhcp.lease_hours)
            # Auto-register unknown MACs so they appear in the dashboard. With
            # guest mode on, a brand-new device becomes a GUEST account instead
            # of a normal managed user.
            existing = await self.database.get_device(mac=mac)
            if existing is None:
                # Blacklisted MACs (manual delete / Network-tab deny list) never
                # auto-register, whatever their ownership state. Fetched only
                # for unknown MACs (a tiny query on a ~15 s tick).
                deny_list = set(await self.database.get_mac_list("deny"))
                # A manually-deleted device/user stays deleted while its device
                # is still on the network: the delete wrote the MAC to the
                # deny list (permanent blacklist), and run.py must NOT
                # auto-register it again. Checked FIRST, before the guest-mode
                # branch: gating on guest mode would re-register a blacklisted
                # device as a NORMAL user the moment guest mode is turned off
                # while the device is still connected — violating "stays
                # deleted while present". The lease binding above is kept, so
                # the engine still sees the IP and keeps dropping the MAC's
                # packets (snapshot_state's row-less deny pass). Un-blacklisting
                # the MAC in the Network tab is the ONLY way back in.
                if mac.lower() in deny_list:
                    log.info("blacklisted MAC %s stays deleted — not "
                             "re-registered (%s)", mac, ip)
                    return
                if await self.service.stop_new_connections():
                    # "STOP NEW CONNECTIONS": a brand-new MAC is refused at
                    # the DHCP level — dnsmasq ignores it, so it never gets
                    # an IP to block and never registers (no "unsigned user"
                    # row). The lease just handed out before the box learned
                    # the MAC is kernel-cut via snapshot_state's row-less
                    # refused pass until it expires. Already-registered
                    # devices are untouched (they never reach this branch).
                    added = await self.service.add_refused_mac(mac)
                    if added:
                        ok = await self._sync_refuse_fragment()
                        if ok:
                            await self.database.add_event(
                                f"New connection refused at DHCP: {mac} "
                                f"({ip}) — STOP NEW CONNECTIONS is on",
                                "warn")
                        else:
                            # Fragment unavailable (no root / no dnsmasq
                            # dir): fall back to the legacy registered +
                            # admin-blocked path so the device is still
                            # controlled.
                            dev = await self.database.upsert_device(
                                mac, name="", quota_mode=_db.QUOTA_DISABLED,
                                fixed_gb=0.0)
                            await self.database.set_device_state(
                                dev.id, _db.BLOCK_ADMIN)
                            await self.database.add_event(
                                f"New connection blocked: {dev.mac} ({ip}) "
                                f"— STOP NEW CONNECTIONS is on (DHCP "
                                f"refusal unavailable)", "warn", dev.id)
                    return
                elif (await self.service.decline_random_macs()
                        and self.service.is_random_mac(mac)):
                    # "Decline random MACs": a brand-new device with a
                    # randomized (locally-administered) privacy MAC is refused
                    # at the DHCP level — dnsmasq ignores it, so it never gets
                    # an IP to block and never registers (no "unsigned user"
                    # row). Random MACs carry no vendor OUI, so the box can't
                    # identify or budget them; the admin gates them until they
                    # rejoin with their real address. A real (globally-unique)
                    # MAC never reaches this branch. The lease just handed out
                    # is kernel-cut via snapshot_state's row-less refused pass.
                    added = await self.service.add_refused_random_mac(mac)
                    if added:
                        ok = await self._sync_refuse_fragment()
                        if ok:
                            await self.database.add_event(
                                f"New connection refused at DHCP: {mac} "
                                f"({ip}) — randomized (privacy) MAC declined",
                                "warn")
                        else:
                            # Fragment unavailable (no root / no dnsmasq
                            # dir): fall back to the legacy registered +
                            # admin-blocked path so the device is still
                            # controlled.
                            dev = await self.database.upsert_device(
                                mac, name="", quota_mode=_db.QUOTA_DISABLED,
                                fixed_gb=0.0)
                            await self.database.set_device_state(
                                dev.id, _db.BLOCK_ADMIN)
                            await self.database.add_event(
                                f"New connection blocked: {dev.mac} ({ip}) — "
                                f"randomized (privacy) MAC declined (DHCP "
                                f"refusal unavailable)", "warn", dev.id)
                    return
                elif await self.service.is_guest_mode():
                    gq = await self.service.guest_quota_gb()
                    dev = await self.database.upsert_device(
                        mac, name="", quota_mode=_db.QUOTA_FIXED,
                        fixed_gb=gq, guest=True)
                    limit = await self.service.guest_limit()
                    if await self.database.count_guest_users() > limit:
                        # Guest cap reached: the account is still minted (so
                        # the MAC is visible + counted) but immediately cut —
                        # a MAC-changer can't spam fresh allowances forever.
                        await self.database.set_device_state(
                            dev.id, _db.BLOCK_ADMIN)
                        await self.database.add_event(
                            f"New GUEST blocked: {dev.mac} ({ip}) — "
                            f"guest limit ({limit}) reached", "warn", dev.id)
                    else:
                        await self.database.add_event(
                            f"New GUEST device on network: {dev.mac} ({ip}) — "
                            f"{gq:g} GB allowance", "info", dev.id)
                else:
                    # Brand-new device: mint its user in the DISABLED
                    # onboarding lock — 0 GB, no auto share, kernel-cut until
                    # the admin assigns shared or fixed in the modal.
                    dev = await self.database.upsert_device(
                        mac, name="", quota_mode=_db.QUOTA_DISABLED,
                        fixed_gb=0.0)
                    await self.database.add_event(
                        f"New device on network: {dev.mac} ({ip}) — "
                        f"quota unassigned (0 GB, awaiting admin)",
                        "info", dev.id)
                # A brand-new device must receive its allowance BEFORE the
                # next evaluate_blocks run, or allowances.get(mac) returns 0.0
                # and the device is instantly blocked for "quota exceeded".
                await self.service.recompute_allowances()
                self._new_devices_detected = True
                log.info("auto-registered new device %s (%s)", mac, ip)
        except Exception:  # noqa: BLE001
            log.exception("failed to persist lease %s -> %s", mac, ip)

    async def _sync_dnsmasq_leases(self) -> None:
        """Learn MAC<->IP bindings from dnsmasq's lease file.

        dnsmasq owns DHCP on the gateway, so we parse its lease file every
        maintenance tick and feed each binding through :meth:`_persist_lease`
        (DB upsert + auto-register unknown devices + allowances).
        """
        path = Path(self.cfg.dhcp.lease_file)
        if not path.exists():
            return
        try:
            text = await asyncio.to_thread(
                lambda: path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            log.warning("cannot read dnsmasq leases %s: %s", path, exc)
            return
        seen: set[str] = set()
        for line in text.splitlines():
            parts = line.split()
            # format: <expiry-epoch> <mac> <ip> [hostname] [clientid]
            if len(parts) < 3:
                continue
            mac, ip = parts[1], parts[2]
            if mac in seen:
                continue
            seen.add(mac)
            await self._persist_lease(mac, ip)
        # Prune DB bindings for MACs dnsmasq no longer knows about (their lease
        # expired and wasn't renewed). Guarded on `seen` being non-empty so a
        # transiently empty lease file (dnsmasq just restarted) never wipes
        # every binding — that would un-account every device until its DHCP
        # renew and mis-count the month.
        if seen:
            db_leases = await self.database.list_leases()
            for lease in db_leases:
                if lease.mac not in seen:
                    await self.database.delete_lease(lease.mac)
            # The deny list is NOT pruned here: a blacklisted MAC stays
            # blacklisted until the admin removes it in the Network tab, even
            # if the device left the network in between (permanent blacklist).
        if self._new_devices_detected:
            self._new_devices_detected = False
            self._last_latency_sweep = 0
            log.debug("new device detected — scheduling immediate ARP sweep")

    async def _collect_neigh_active(self) -> None:
        """Collect IPs with an active kernel neighbor state into
        ``_neigh_active_ips``.

        ``ip -j neigh`` returns one entry per resolved neighbor: IP + state.
        Anything except FAILED/INCOMPLETE means the kernel recently spoke to
        that neighbor — these reinforce the ARP probe for the LED: a device
        that missed the probe burst but is generating traffic still has a
        valid ARP mapping.

        Parsing falls back to plain ``ip neigh`` text when ``-j`` is
        unavailable (old iproute2). Neighbor states that carry no usable MAC
        (FAILED/INCOMPLETE) are skipped. IPv6 rows are skipped (the quota model
        is IPv4). A missing/unusable ``ip`` binary or a non-Linux box degrades
        to a no-op — the LED just falls back to the probe + lease data.
        """
        leases = await self.database.list_leases()
        if not leases:
            return
        wanted = {l.ip for l in leases}
        active: set[str] = set()
        code, out = await asyncio.to_thread(
            self._ip_run, ["ip", "-j", "neigh"])
        if code == 0 and out:
            for row in _iter_json_neigh(out):
                dst = row.get("dst") or ""
                if ":" in dst or dst not in wanted:
                    continue
                if row.get("state") in ("FAILED", "INCOMPLETE"):
                    continue
                active.add(dst)
        else:
            # Fallback: `ip neigh` text (older iproute2). Same filters.
            code2, out2 = await asyncio.to_thread(
                self._ip_run, ["ip", "neigh"])
            if code2 == 0 and out2:
                for line in out2.splitlines():
                    fields = line.split()
                    if len(fields) < 2 or ":" in fields[0]:
                        continue
                    dst = fields[0]
                    if dst not in wanted:
                        continue
                    if "FAILED" in fields or "INCOMPLETE" in fields:
                        continue
                    active.add(dst)
        self._neigh_active_ips = active

    async def _wifi_probe_tick(self) -> None:
        """Collect a snapshot from the WiFi probe (monitor-mode radio data).
        The snapshot drives the yield check in _maybe_latency_tick: when the
        monitor probe is available, the ARP-RTT probe yields.
        """
        probe = self.wifi_probe
        if probe is None:
            return
        snap = await asyncio.to_thread(probe.snapshot)
        self._wifi_probe_state = snap

    async def _maybe_latency_tick(self) -> None:
        """Probe every leased client by ARP round-trip time.

        Runs on its own cadence (``network.latency_probe.interval_s``) and
        never on the loop: the raw-socket probe sleeps between sends, so it
        lives in a worker thread. The probe result drives the device-card LED
        (connected NOW): IPs that answered the latest sweep (plus IPs with
        active kernel neighbor entries) are blue; others are grey. When the
        monitor probe is available it takes precedence — the latency tick
        yields.
        """
        probe = self.latency_probe
        if probe is None:
            return
        now = time.monotonic()
        if now - self._last_latency_sweep < self._latency_interval:
            return
        self._last_latency_sweep = now
        if self._wifi_probe_state.get("available"):
            return  # monitor mode knows more (SSIDs) — never fight it
        leases = await self.database.list_leases()
        targets = [l.ip for l in leases if l.ip]
        if not targets:
            return
        try:
            rtts = await asyncio.to_thread(probe.probe, targets)
        except Exception:  # noqa: BLE001
            log.exception("latency probe failed")
            return
        # Every completed sweep refreshes the responder map.  An empty sweep
        # (0 replies from N targets) is treated as a probe-degradation event
        # — NOT as "nobody is online" — because the probe backend can
        # silently return {} (NIC resolution failure, raw-socket loss,
        # power-save devices all sleeping through the burst).  The old map is
        # kept; it goes stale after 3×interval and the freshness gate falls
        # back to lease-based connectedness.  An *exception* already keeps
        # the old map (line 987-989); this closes the clean-empty loophole.
        self._latency_sweep_ran = True
        if rtts:
            self._latency_responders = (
                set(rtts) | self._neigh_active_ips)
        # Even when the ARP probe returned results, merge the kernel neighbor
        # table: a device that missed the ARP burst but is generating traffic
        # still has a valid REACHABLE/STALE entry — it stays blue.
        elif self._neigh_active_ips:
            self._latency_responders = set(self._neigh_active_ips)
        if not rtts and not self._neigh_active_ips:
            return

    def _latency_active_ips(self) -> Optional[set[str]]:
        """IPs that answered the latest ARP sweep, or None when the probe
        isn't running / data is stale (caller falls back to lease-based
        connectedness). Drives the device-card LED: a leased device that
        stopped answering (asleep, off, or on another network) goes grey."""
        if self.latency_probe is None or not self._latency_sweep_ran:
            return None
        if (self._latency_interval > 0
                and time.monotonic() - self._last_latency_sweep
                > 3 * self._latency_interval):
            return None  # stale — fall back to lease-based
        return set(self._latency_responders)

    async def _scan_rogues(self) -> None:
        """Probe the LAN for active hosts that are NOT known DHCP devices.

        Off the event loop (raw ARP socket + ping sweep). New rogues are logged
        to the events table so the Activity tab tells the story. The scan result
        is kept in ``self._rogues`` and surfaces in the holder swap every tick;
        a rogue that disappears just drops out on the next scan.
        """
        if self.arp_scanner is None:
            return  # never built (startup ordering) — nothing to scan
        try:
            leases = await self.database.list_leases()
            known = {l.mac for l in leases}
            rogues = await asyncio.to_thread(self.arp_scanner.scan, known)
        except Exception:  # noqa: BLE001
            log.exception("rogue LAN scan failed")
            return
        self._rogues = rogues
        seen = {r.mac for r in rogues}
        for r in rogues:
            if r.mac not in self._known_rogue_macs:
                await self.database.add_event(
                    f"Rogue device on network: {r.mac} ({r.ip}) — active but "
                    f"not a DHCP client (static IP / router-gateway bypass); "
                    f"quota cannot count or block it",
                    "warning", None)
        self._known_rogue_macs = seen

    async def _dns_history_tick(self) -> None:
        """Drain the DNS-history queue into per-device buckets, then prune.

        The tailer thread (quota.dnslog) already did the file I/O; this only
        drains its bounded queue (non-blocking), resolves each distinct IP to
        a device once, and batch-upserts the per-minute/domain counts. Runs on
        every tick while the tailer is alive. No ``asyncio.to_thread`` needed —
        there is no blocking call left in this path.
        """
        events = self.dnslog.drain_events()
        if not events:
            return
        # Resolve each distinct requestor IP to a device once per drain.
        device_by_ip: dict[str, int | None] = {}
        for ev in events:
            if ev.ip not in device_by_ip:
                dev = await self.database.get_device_by_ip(ev.ip)
                device_by_ip[ev.ip] = dev.id if dev else None
        # Aggregate into (device_id, minute, domain) -> count buckets; an IP
        # with no device (rogue / lease gap at drain time) is skipped — the
        # same attribution rule the byte counters use.
        buckets: dict[tuple[int, str, str], int] = {}
        for ev in events:
            dev_id = device_by_ip.get(ev.ip)
            if dev_id is None:
                continue
            key = (dev_id, ev.minute, ev.domain)
            buckets[key] = buckets.get(key, 0) + 1
        if buckets:
            await self.database.batch_add_dns_history(
                [(k[0], k[1], k[2], v) for k, v in buckets.items()])
        # Persist the read cursor so a restart resumes without re-reading.
        await self.database.set_setting(
            "dnslog_state", json.dumps(self.dnslog.state_snapshot()))
        # Hourly TTL gate: prune each user's history at THEIR retention.
        now = time.monotonic()
        if now - self._last_dns_prune >= self.DNS_PRUNE_INTERVAL:
            self._last_dns_prune = now
            await self._prune_dns_history()

    async def _prune_dns_history(self) -> None:
        """Delete DNS-history rows older than each user's retention cutoff.

        Per-user ``history_days`` (NULL = the global ``cfg.history.
        retention_days``) decides the cutoff minute. Each user is pruned with
        their OWN cutoff (the DB method scopes the delete per user, so a short
        retention never wipes a longer one). Runs on the hourly gate, so a
        per-user retention edit reaches the DB within the hour.
        """
        try:
            global_days = int(getattr(self.cfg.history, "retention_days", 7) or 7)
        except (ValueError, TypeError):
            global_days = 7
        try:
            users = await self.database.list_users()
        except Exception:  # noqa: BLE001
            log.exception("dns history prune: list_users failed")
            return
        now_minute = _dt.datetime.now().astimezone()
        for u in users:
            days = u.history_days if u.history_days is not None else global_days
            cutoff = (now_minute - _dt.timedelta(days=days)
                      ).strftime("%Y-%m-%d %H:%M")
            try:
                deleted = await self.database.prune_dns_history(u.id, cutoff)
            except Exception:  # noqa: BLE001
                log.exception("dns history prune failed for user %s", u.id)
                continue
            if deleted:
                log.info("dns history prune: deleted %s rows older than "
                         "%s (user %s)", deleted, cutoff, u.id)

    # ---------------------------------------------------------- maintenance

    async def _maintenance_loop(self) -> None:
        """Periodic flush + block re-evaluation + engine state sync."""
        interval = 15.0
        while not self._stop_event.is_set():
            try:
                started = time.perf_counter()
                await self._maintenance_tick()
                elapsed = time.perf_counter() - started
                if elapsed > 1.0:
                    # Timing telemetry: the tick must never hold the event
                    # loop (WS pushes + API requests starve). >1 s means a
                    # subprocess is back on-loop or the DB is thrashing —
                    # surface it as a warning, not a silent stall.
                    log.warning("maintenance tick took %.2fs (>1.0s)", elapsed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("maintenance tick failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _maintenance_tick(self) -> None:
        # 1. Roll the quota period if stale (covers month boundary).
        await self.service.ensure_period()

        # 1b. Learn device bindings from dnsmasq's lease file.
        await self._sync_dnsmasq_leases()

        # 1b2. Learn each device's source NIC (NIC tag) from the kernel
        #      neighbor table. Only updates when a fresh value is seen, so an
        #      offline device keeps its last-known interface.
        await self._collect_neigh_active()

        # 1b3. Passive WiFi probe: collects monitor-mode snapshots for the
        #      yield check in _maybe_latency_tick (when the monitor probe is
        #      available, the ARP-RTT probe yields).
        await self._wifi_probe_tick()

        # 1b4. ARP-RTT probe (any hardware). Slow cadence,
        #      off the event loop (raw sockets + sleeps in a worker thread).
        await self._maybe_latency_tick()

        # 1c. Rogue/unmanaged-host scan on a slow cadence (60 s): static-IP
        #     devices are not in the lease file, so they never show in the
        #     dashboard. Active-but-unleased hosts are surfaced as rogues.
        now = time.monotonic()
        if now - self._last_rogue_scan >= self.ROGUE_SCAN_INTERVAL:
            self._last_rogue_scan = now
            await self._scan_rogues()

        # 1d. Drain dnsmasq's query log into per-device DNS history (the
        #     tailer thread did the file I/O; this only drains its queue).
        if self.dnslog is not None and self.dnslog.running:
            await self._dns_history_tick()

        # 1e. Prune the audit trail (events table) on an hourly gate — the
        #     only real disk-growth risk on a months-long gateway.
        if now - self._last_events_prune >= self.EVENTS_PRUNE_INTERVAL:
            self._last_events_prune = now
            try:
                deleted = await self.database.prune_events(
                    time.time() - self.EVENTS_RETENTION_SEC)
            except Exception:  # noqa: BLE001
                log.exception("events prune failed")
            else:
                if deleted:
                    log.info("events prune: deleted %s rows older than "
                             "%d days", deleted,
                             self.EVENTS_RETENTION_SEC // 86400)

        # 1f. WAN public-IP auto-renew on schedule (the dashboard WAN tab).
        #     Only in WAN mode with ppp0 UP — a down dial means the internet
        #     isn't working, and renewing into a dead line is pointless.
        await self._wan_ip_renew_tick()

        # 1g. GitHub self-update check (24 h gate lives inside the updater —
        #     maybe_check is a no-op until the interval elapses). A failure is
        #     swallowed inside; the state surfaces in the Admin tab. None when
        #     cfg.updates.enabled=false (master switch / hermetic tests).
        if self.updater is not None:
            try:
                await self.updater.maybe_check()
            except Exception:  # noqa: BLE001 — never let the updater stall a tick
                log.exception("update check tick failed")

        # 2. Drain the packet engine's counters into usage_daily.
        #    flush() shells out to `nft -j list counters` (a subprocess). Run
        #    it off the event loop so a slow nft never stalls the WebSocket
        #    push or the API.
        live_by_ip: dict[str, object] = {}
        if self.engine is not None:
            snap = await asyncio.to_thread(self.engine.flush)
            live_by_ip = snap.by_ip
            today = _dt.date.today().isoformat()
            if snap.by_ip:
                for ip, counters in snap.by_ip.items():
                    if counters.up == 0 and counters.down == 0:
                        continue
                    mac = snap.ip_to_mac.get(ip)
                    if not mac:
                        mac = await self.database.get_mac_for_ip(ip)
                    if not mac:
                        continue  # IP not tied to a known device
                    dev = await self.database.get_device(mac=mac)
                    if dev is None:
                        continue
                    await self.database.add_usage(
                        dev.id, today, counters.up, counters.down)
            # The box's OWN internet (input/output hooks, q_gw_* counters):
            # charged to the protected "Gateway" user's device so the machine's
            # bundle consumption sits inside the quota math. The box's MAC has
            # no lease, so it never appears in snap.by_ip — only in snap.gateway.
            if snap.gateway.up or snap.gateway.down:
                box = await self.database.get_device(mac=GATEWAY_MAC)
                if box is not None:
                    await self.database.add_usage(
                        box.id, today, snap.gateway.up, snap.gateway.down)

        # 3. Recompute block states from usage vs allowances.
        changes = await self.service.evaluate_blocks()
        for ch in changes:
            log.warning("device %s (%s) -> %s", ch["device_id"], ch["mac"], ch["state"])

        # 4. Push fresh enforcement maps into the engine + holder.
        state = await self.service.snapshot_state()
        ip_to_mac = {v["ip"]: mac for mac, v in state.items() if v.get("ip")}
        self._last_ip_to_mac = ip_to_mac  # for an immediate shaping re-sync
        blocked = {mac: v["blocked"] for mac, v in state.items()}
        engine_available = False
        gateway_blocked = None
        if self.engine is not None:
            # nft update_state + set_gateway_blocked shell out to `nft`
            # (a subprocess per rebuild, ~80 on a first boot). Run them off
            # the event loop so a slow kernel sync never stalls the WS push.
            await asyncio.to_thread(self.engine.update_state, ip_to_mac, blocked)
            # The box's own internet is cut via the gateway chains (gw_blocked
            # set) — its MAC has no lease/IP, so it never enters the forward
            # blocked set above. The Gateway user's resolved state drives the
            # toggle; the engine cache-gates a no-op on an unchanged state.
            await asyncio.to_thread(
                self.engine.set_gateway_blocked,
                bool(state.get(GATEWAY_MAC, {}).get("blocked", False)))
            # getattr: some test fakes implement only the minimal engine
            # surface; the real engine always carries both attributes.
            engine_available = bool(getattr(self.engine, "available", True))
            # What the kernel actually holds for the box's cut (True/False), or
            # None if it was never programmed — copied into the snapshot so the
            # dashboard can show "Blocked in the UI but not cut at the kernel".
            gateway_blocked = getattr(self.engine, "gateway_blocked", None)
        # Live counters = the flushed delta, so the dashboard shows real
        # recent traffic instead of always-zero values.
        self.holder.swap(EngineSnapshot(
            by_ip=live_by_ip,
            ip_to_mac=ip_to_mac, blocked=blocked, rogue=list(self._rogues),
            wan_status=await self._wan_status(),
            gateway_blocked=gateway_blocked,
            engine_available=engine_available,
            ts=time.time()))

        # 5. Linux: reconcile the tc speed-shaping tree (settings + live IPs).
        #    update_state is signature-gated, so an unchanged state does not
        #    touch the kernel; a DB edit lands here within one tick (<=15 s).
        await self._sync_shaping(ip_to_mac)

        # 6. Regenerate dnsmasq's domain-rule + tag config if anything
        #    changed (new device -> new tag, or a rule/preset/DNS-server
        #    edit that arrived without going through the API's immediate
        #    apply — e.g. a test or a future non-HTTP caller).
        await self._sync_dns_rules()

        # 6b. Reconcile the DHCP-refusal fragment for the STOP-NEW + Decline-random
        #     gates (cheap content diff; self-heals a gate toggle that missed
        #     the API's immediate apply, a deleted fragment, or a leftover
        #     fragment after a gate was turned off).
        await self._sync_refuse_fragment()

        # 7. Reconcile the VPN-share policy routing (cheap when nothing
        #    changed; re-applies when the VPN client restarts/re-creates
        #    its tunnel, and removes the rule if the tunnel vanished so
        #    clients never ride a dead VPN into a blackhole).
        await self._sync_vpn_share()

        # 8. Reconcile the kernel firewall (quota_firewall). Re-applies ONLY
        #    when the stored config or the topology changed (signature-gated),
        #    drains the scan-watch + fw_* counters into the Firewall log view,
        #    and persists any ban/drop events. All nft work runs off the loop.
        if self.firewall is not None \
                and getattr(self.firewall, "available", False):
            try:
                await self.firewall.reconcile()
            except Exception:  # noqa: BLE001 — never let the firewall stall a tick
                log.exception("firewall reconcile failed")

    async def _sync_shaping(self, ip_to_mac: dict[str, str]) -> None:
        """Push the latest shaping settings + device caps into the tc shaper.

        The rate map holds one entry per device that has a live IP: its own
        caps (``down``/``up``) and its user's aggregate caps (``user_down`` /
        ``user_up``). Devices without a live IP cannot be shaped (tc matches
        IPs), so they stay out until they lease an address.
        """
        shaper = self.shaper
        if shaper is None or not getattr(shaper, "available", False):
            self._shaping_applied = False
            self._shaping_available = False
            return
        try:
            async with self._shaping_lock:
                config = await self.service.get_shaping_config()
                guest_speed = await self.service.guest_speed_limit_mbps()
                users = {u.id: u for u in await self.database.list_users()}
                devices = {d.mac: d for d in await self.database.list_devices()}
                rate_map: list[dict[str, object]] = []
                for ip, mac in ip_to_mac.items():
                    dev = devices.get(mac)
                    if dev is None or dev.user_id is None:
                        continue  # untracked or orphaned — nothing to shape
                    user = users.get(dev.user_id)
                    user_down = float(user.limit_down_mbps or 0.0) if user else 0.0
                    user_up = float(user.limit_up_mbps or 0.0) if user else 0.0
                    # A default guest speed cap acts as the guest user's
                    # aggregate ceiling: the shaper already applies
                    # min(device, user), so only the stricter cap wins.
                    if user is not None and user.guest and guest_speed > 0:
                        user_down = min(user_down, guest_speed) if user_down else guest_speed
                        user_up = min(user_up, guest_speed) if user_up else guest_speed
                    rate_map.append({
                        "ip": ip,
                        "device_id": dev.id,
                        "user_id": dev.user_id,
                        "down": float(dev.limit_down_mbps or 0.0),
                        "up": float(dev.limit_up_mbps or 0.0),
                        "user_down": user_down,
                        "user_up": user_up,
                    })
                # A tree rebuild runs ~70-115+ sequential `tc` subprocesses
                # (~1.5-5 s). Keep it OFF the event loop (WS pushes + API
                # requests starve during the rebuild otherwise). The lock
                # still serializes tick vs API-immediate re-syncs, and
                # update_state's signature gate makes the common no-op free.
                await asyncio.to_thread(
                    shaper.update_state,
                    rate_map, config["enabled"],
                    config["total_down_mbps"],
                    config["total_up_mbps"],
                    lan_rate_mbps=config["lan_rate_mbps"])
                self._shaping_applied = bool(getattr(shaper, "applied", True))
                self._shaping_available = bool(
                    getattr(shaper, "available", False))
        except Exception:  # noqa: BLE001
            log.exception("failed to sync speed-shaping rules")
            self._shaping_applied = False

    async def _reshaping_now(self) -> None:
        """Apply a speed-limit edit immediately instead of waiting for the next
        15 s maintenance tick. The API schedules this after a Network-tab save
        or a device/user cap edit; ``update_state`` is signature-gated, so a
        no-op call is free and the tick's next call still sees the same state.
        """
        # Pre-tick fallback: before the first maintenance tick the live
        # ip->mac map is empty, so an immediate re-sync after a Network-tab
        # save would feed the shaper an empty map and the preview would show
        # stale (or zero) caps. Rebuild it from the DB state when missing.
        if not self._last_ip_to_mac:
            try:
                state = await self.service.snapshot_state()
                self._last_ip_to_mac = {
                    v["ip"]: mac for mac, v in state.items() if v.get("ip")}
            except Exception:  # noqa: BLE001
                log.exception("_reshaping_now: snapshot_state fallback failed")
        try:
            await self._sync_shaping(self._last_ip_to_mac)
        except Exception:  # noqa: BLE001
            log.exception("failed to apply speed-limit change immediately")

    def _shaping_state(self) -> dict[str, object]:
        """Live shaping state for the WS snapshot + /api/network preview."""
        return {
            "available": self._shaping_available,
            "applied": self._shaping_applied,
        }

    async def _sync_dns_rules(self) -> None:
        """Regenerate the dnsmasq domain-rule + tag config from the DB and
        reload dnsmasq if anything changed.

        Mirrors ``_sync_shaping``: reads devices/users/rules fresh every
        call and lets ``DnsRuleManager.apply`` decide (by content diff)
        whether a reload is actually needed, so a call with nothing new to
        say is free. Called once per maintenance tick (new devices need a
        tag even with no rule edits) and immediately after any DNS-related
        API edit via ``_apply_dns_now``.
        """
        manager = self.dns_manager
        if manager is None:
            return
        try:
            async with self._dns_lock:
                devices = await self.database.list_devices()
                users = {u.id: u for u in await self.database.list_users()}
                rules = await self.database.list_domain_rules(enabled_only=True)
                device_ids_by_user: dict[int, list[int]] = {}
                for dev in devices:
                    if dev.user_id is not None:
                        device_ids_by_user.setdefault(dev.user_id, []).append(dev.id)
                # Per-client DNS-server overrides: a device's own override
                # wins; otherwise it inherits its user's override (if any).
                # Rendered as (scope, scope_id, ip) triples, same shape as a
                # domain rule's scope so render_rules' _tags_for is reused.
                dns_servers: list[tuple[str, int | None, str]] = []
                for user in users.values():
                    if user.dns_server:
                        dns_servers.append((_db.DNS_SCOPE_USER, user.id, user.dns_server))
                for dev in devices:
                    if dev.dns_server:
                        dns_servers.append((_db.DNS_SCOPE_DEVICE, dev.id, dev.dns_server))
                await asyncio.to_thread(
                    manager.apply, devices, rules, dns_servers, device_ids_by_user)
        except Exception:  # noqa: BLE001
            log.exception("failed to sync DNS filtering rules")

    async def _apply_dns_now(self) -> None:
        """Apply a domain-rule / preset / DNS-server edit immediately instead
        of waiting for the next maintenance tick. The API schedules this
        after every /api/dns/* and /api/{users,devices}/{id}/dns edit."""
        await self._sync_dns_rules()

    async def _sync_refuse_fragment(self) -> bool:
        """Reconcile the DHCP-refusal fragment with BOTH gates' refused sets.

        With STOP NEW CONNECTIONS on, every refused MAC gets a
        ``dhcp-host=<mac>,ignore`` line so dnsmasq never hands it an IP;
        "Decline random MACs" feeds the same fragment with its own list.
        Each gate's off clears only its own refused list (the other gate's
        refusals stay), then the fragment is emptied. Writes only on a
        content change and restarts dnsmasq only then (same pattern as
        DnsRuleManager). Returns False when the fragment could not be
        written — the caller falls back to the legacy registered-admin-block
        so the device stays controlled.
        """
        refused = set()
        if await self.service.stop_new_connections():
            refused |= await self.service.refused_macs()
        else:
            await self.service.clear_refused_macs()
        if await self.service.decline_random_macs():
            refused |= await self.service.refused_random_macs()
        else:
            await self.service.clear_refused_random_macs()
        if not self.cfg.dhcp.enable:
            # No dnsmasq on this boot: nothing to refuse at the DHCP level
            # (the refuse-list row-less kernel block still applies). Tests
            # and non-gateway boots never touch the fragment path.
            return True
        return await asyncio.to_thread(
            self._refuse_fragment_sync,
            Path(self.cfg.dhcp.ignore_file), refused,
            self.cfg.dhcp.reload_dnsmasq)

    @staticmethod
    def _refuse_fragment_sync(ignore_file: Path, refused: set[str],
                              reload_dnsmasq: bool) -> bool:
        """Write one ``dhcp-host=<mac>,ignore`` line per refused MAC (or an
        empty file when the set is empty), restarting dnsmasq when the
        content actually changed. True when the file now matches the set;
        False when the write failed (unwritable path — no root / no
        dnsmasq dir on a non-gateway boot)."""
        content = "".join(f"dhcp-host={m},ignore\n" for m in sorted(refused))
        try:
            existing = (ignore_file.read_text(encoding="utf-8")
                        if ignore_file.exists() else None)
        except OSError:
            existing = None
        if existing == content:
            return True
        try:
            ignore_file.parent.mkdir(parents=True, exist_ok=True)
            ignore_file.write_text(content, encoding="utf-8")
        except OSError as exc:
            log.error("could not write %s: %s — DHCP refusal fragment not "
                      "applied (run as root on the gateway?)", ignore_file, exc)
            return False
        if reload_dnsmasq:
            _reload_dnsmasq()
        return True

    async def _apply_stop_new_now(self) -> None:
        """Apply a STOP-NEW-CONNECTIONS toggle immediately instead of waiting
        for the next maintenance tick. The API schedules this after every
        /api/guest {stop_new: ...} edit."""
        await self._sync_refuse_fragment()

    async def _apply_decline_random_now(self) -> None:
        """Apply a Decline-random-MACs toggle immediately instead of waiting
        for the next maintenance tick. The API schedules this after every
        /api/network {decline_random_macs: ...} edit."""
        await self._sync_refuse_fragment()

    async def _vpn_bypass_ips(self) -> list[str]:
        """Client IPs excluded from the VPN-share tunnel (per-device/user
        ``vpn_bypass``).

        Effective flag = the device's own flag OR its owning user's flag;
        only devices holding a LIVE IP matter (policy rules match source
        addresses), so lease-less rows are skipped. Runs two DB reads —
        called once per tick while the share is enabled, skipped entirely
        when it is off.
        """
        snap = self.holder.get()
        if not snap.ip_to_mac:
            return []
        devices = {d.mac: d for d in await self.database.list_devices()}
        users = {u.id: u for u in await self.database.list_users()}
        excluded: set[str] = set()
        for ip, mac in snap.ip_to_mac.items():
            if not ip:
                continue
            dev = devices.get(mac)
            if dev is None:
                continue
            user = users.get(dev.user_id) if dev.user_id is not None else None
            if dev.vpn_bypass or (user is not None and user.vpn_bypass):
                excluded.add(ip)
        return sorted(excluded)

    @staticmethod
    def _arbitrate_vpn_tunnel(manager: object, pin: str,
                              bridge_iface: str) -> str:
        """Which tunnel should VPN share prefer this tick?

        The persisted pin keeps a healthy single-tunnel box stable, but it
        must not outvote a FRESHER tunnel: switching the VPN server or client
        leaves the old tunnel device alive alongside the new one, and routing
        by the old name then keeps the HOUSEHOLD on the stale server forever
        while the box itself exits via the new one (devices keep showing the
        old VPN IP). When zero/one tunnel is live nothing changes — no extra
        probes beyond one sysfs scan — and ``pin`` is returned UNCHANGED so
        all legacy semantics (pin stability, cfg_iface precedence inside the
        manager) hold. With several live tunnels, the NEWEST tunnel that is
        not our own tun2socks bridge device wins (higher ifindex = dialed
        later), and the multi-tunnel state is logged loudly so a
        stale/zombie session is diagnosable. An injected manager without
        ``detect_interfaces`` (wiring-test fakes) passes the pin through."""
        detect = getattr(manager, "detect_interfaces", None)
        if detect is None:
            return pin
        try:
            cands = sorted(set(detect()))
        except Exception:  # noqa: BLE001
            return pin
        if len(cands) <= 1:
            return pin
        idx_fn = getattr(manager, "ifindexes", None)
        idx = (idx_fn() or {}) if idx_fn is not None else {}
        foreign = [c for c in cands if not bridge_iface or c != bridge_iface]
        if not foreign:
            return pin
        chosen = max(foreign, key=lambda n: (idx.get(n, 0), n))
        log.warning(
            "vpn share: multiple live tunnels (%s) — using %s%s",
            ", ".join(f"{n}#{idx.get(n, '?')}" for n in cands),
            chosen,
            "" if chosen == pin else " — the pinned tunnel was superseded")
        return chosen

    async def _sync_vpn_share(self) -> None:
        """Reconcile the VPN-share policy routing with the dashboard switch.

        Reads the DB setting + pinned tunnel, asks the kernel-side manager
        to reconcile (idempotent: apply when on, remove when off, self-heal
        leftovers), persists the detected tunnel as the pin (so a multi-VPN
        / restarted-tunnel box re-applies the SAME interface — superseded
        only when SEVERAL live tunnels exist and a fresher non-bridge one
        appeared, so switching VPN servers can't strand devices on the old
        one), and keeps the engine's gateway cut in step (while relaying,
        the box's own internet
        can be cut — Gateway OFF — and ONLY the VPN server endpoint(s) the
        relay rides stay reachable via the ``gw_allowed`` whitelist, so the
        household's tunnel survives; see NftablesEngine.set_gateway_allowed).
        While enabled, per-device/user ``vpn_bypass`` IPs are computed here
        and handed to the manager so those devices ride the direct uplink.
        The routing manager is reconciled FIRST so a REAL kernel tunnel
        (xray/sing-box/WireGuard tun) always wins and needs no config edits.
        The tun2socks bridge (for userspace-netstack clients like v2rayN,
        which never create a kernel tun) is only a FALLBACK: it is engaged
        only when the routing found no tunnel, and a leftover bridge child is
        stopped once a different real tunnel carries the subnet (never when
        the route rides the bridge's OWN device — that would blackhole it).
        All subprocesses run off the event loop; a manager-less boot (vpn_share
        disabled in config) is a no-op.
        """
        manager = self.vpn_manager
        if manager is None:
            return
        try:
            async with self._vpn_lock:
                enabled = (await self.database.get_setting(
                    "vpn_share_enabled", "0")) == "1"
                db_pin = (await self.database.get_setting(
                    "vpn_share_interface", "") or "").strip()
                pin = db_pin
                # Devices/users marked vpn_bypass keep riding the direct
                # uplink while everyone else shares the tunnel.
                bypass_ips: list[str] = (
                    await self._vpn_bypass_ips() if enabled else [])
                # Prefer a REAL kernel tunnel (xray/sing-box/WireGuard tun):
                # reconcile the routing manager FIRST so its auto-detect wins.
                # The tun2socks bridge is only the FALLBACK for userspace
                # clients (v2rayN never creates a kernel tun) — engage it only
                # when the routing found no tunnel, and stop a leftover bridge
                # once a different real tunnel carries the subnet.
                ts_status = None
                bridge_iface = (self.tun2socks_manager.interface
                                if self.tun2socks_manager is not None else "")
                if enabled:
                    # A stale pin must never outvote a fresher tunnel: a VPN
                    # client/server switch spawns the new tunnel ALONGSIDE
                    # the old one, and pinning the old name would keep the
                    # household on the stale server while the box itself
                    # already exits via the new one.
                    pin = await asyncio.to_thread(
                        self._arbitrate_vpn_tunnel, manager, pin,
                        bridge_iface)
                status = await asyncio.to_thread(
                    manager.reconcile, enabled, pin, bypass_ips)
                if enabled and status.state != "on":
                    # No kernel tunnel was found/routed. For a userspace client
                    # auto-provision the bridge and retry the routing pinned to
                    # its device; otherwise report no-interface honestly.
                    if self.tun2socks_manager is not None:
                        ts_status = await asyncio.to_thread(
                            self.tun2socks_manager.reconcile, True)
                        if (ts_status.state == "running"
                                and ts_status.interface):
                            status = await asyncio.to_thread(
                                manager.reconcile, True,
                                ts_status.interface, bypass_ips)
                elif enabled and self.tun2socks_manager is not None:
                    # A real tunnel is carrying the subnet. The bridge's own
                    # device is legit (its child owns that tun — stopping it
                    # would blackhole the route); any OTHER bridge leftover is
                    # redundant now — stop it so the tunnel detector is clean.
                    if status.interface != bridge_iface:
                        ts_status = await asyncio.to_thread(
                            self.tun2socks_manager.reconcile, False)
                    else:
                        ts_status = await asyncio.to_thread(
                            self.tun2socks_manager.reconcile, True)
                elif not enabled and self.tun2socks_manager is not None:
                    ts_status = await asyncio.to_thread(
                        self.tun2socks_manager.reconcile, False)
                if status.state == "on" and db_pin != status.interface:
                    await self.database.set_setting(
                        "vpn_share_interface", status.interface)
                    log.info("vpn share: pinned tunnel interface %s",
                             status.interface)
                elif not enabled and db_pin:
                    # Clear the pin on OFF so the next ON re-detects from
                    # scratch — toggling the dashboard switch is then a
                    # reliable recovery after a stale/zombie tunnel pinned a
                    # dead interface name.
                    await self.database.set_setting("vpn_share_interface", "")
                    log.info("vpn share: cleared pinned tunnel interface")
                # While relaying, keep ONLY the box's VPN-server connection(s)
                # reachable under a Gateway cut — the relay rides them. Learn
                # the VPN client's established peers, union the explicit
                # engine.gateway_allow_ips override, and keep the result STICKY
                # (only add). The whitelist survives the SWITCH being on even
                # when the tunnel is momentarily down (state no-interface /
                # error): a cut box must keep its route to the VPN server or it
                # can never re-dial. Only the switch turning OFF clears it — the
                # cut then blocks the box entirely again. The engine cache-gates
                # a no-op; loopback is exempt structurally.
                if self.engine is not None:
                    allowed: list[str] = []
                    if enabled:
                        learned = await asyncio.to_thread(
                            self._vpn_learn, None)
                        self._vpn_allowed |= learned
                        override = list(
                            getattr(self.cfg.engine, "gateway_allow_ips", [])
                            or [])
                        allowed = sorted(self._vpn_allowed | set(override))
                    else:
                        self._vpn_allowed = set()
                    self.engine.set_gateway_allowed(allowed)
                self._last_vpn_status = {
                    "state": status.state,
                    "interface": status.interface or "",
                    "peer": status.peer or "",
                    "candidates": status.candidates or [],
                    "message": status.message or "",
                    "tun2socks": (None if ts_status is None
                                  else {
                                      "state": ts_status.state,
                                      "message": ts_status.message or "",
                                      "proxy": ts_status.proxy or "",
                                      "interface": ts_status.interface or "",
                                  }),
                }
        except Exception:  # noqa: BLE001
            log.exception("failed to reconcile VPN share")
            self._last_vpn_status = {"state": "error",
                                     "message": "reconcile failed"}

    async def _apply_vpn_now(self) -> None:
        """Apply a VPN-share toggle immediately instead of waiting for the
        next maintenance tick. The API schedules this after a Network-tab
        save that includes the switch (or a VPN-bypass edit)."""
        await self._sync_vpn_share()

    def _vpn_status(self) -> dict[str, object]:
        """Live VPN-share state for /api/network's ``vpn_share.status``.
        No probing here — the maintenance tick owns the subprocesses and
        caches the result."""
        return dict(self._last_vpn_status)

    async def _renew_wan_ip(self) -> dict[str, object]:
        """Restart the box's PPPoE dial to renew the public IP (the WAN-tab
        Restart button + the auto-renew schedule). The dial runs through the
        ``quota-wan-ppp`` systemd unit; restarting it tears the session down
        and re-dials — on a metered Egyptian line the ISP assigns a fresh
        public IP to the new session, the same effect as restarting the
        router. Internet drops for a few seconds while ppp0 comes back up.

        The renewal timestamp is persisted (``wan_ip_renew_last``) so the
        auto-renew countdown restarts from now, manual and scheduled alike —
        a gateway restart mid-schedule never immediately re-renews. Runs the
        ``systemctl`` calls off the event loop; never raises (the injectable
        restarter degrades to ``restarted=False`` with an honest detail).
        """
        async with self._renew_lock:
            result = await asyncio.to_thread(self._pppoe_restart)
            try:
                await self.service.mark_wan_renew()
            except Exception:  # noqa: BLE001
                log.exception("wan renew: failed to record renewal time")
            self._last_wan_renew = dict(result)
            log.info("wan renew: %s", result)
            return result

    async def _wan_ip_renew_tick(self) -> None:
        """Run the WAN auto-renew schedule once per maintenance tick.

        Gates, in order: effective topology is WAN (no ppp0 in LAN mode) ->
        the feature is enabled in the DB -> ppp0 is UP (a down dial means
        internet isn't working — the schedule must not hammer a dead line) ->
        the interval has elapsed since ``wan_ip_renew_last``. The timestamp is
        read from the DB (the ``dnslog_state`` resume pattern), so a gateway
        restart never resets the countdown.
        """
        if getattr(self.cfg.engine, "topology", "lan") != "wan":
            return
        try:
            cfg_ = await self.service.get_wan_renew_config()
        except Exception:  # noqa: BLE001
            log.exception("wan renew: failed to read schedule")
            return
        if not cfg_["enabled"]:
            return
        link = await asyncio.to_thread(detect_ppp, "ppp0")
        if link["state"] != "up":
            return
        last = cfg_["last"] or ""
        if not last:
            # Never renewed before: fire now so the countdown starts somewhere.
            await self._renew_wan_ip()
            return
        try:
            last_dt = _dt.datetime.fromisoformat(last)
        except ValueError:
            last_dt = None
        if last_dt is None or last_dt.tzinfo is None:
            await self._renew_wan_ip()
            return
        elapsed = (_dt.datetime.now(_dt.timezone.utc) - last_dt).total_seconds()
        if elapsed >= cfg_["minutes"] * 60:
            await self._renew_wan_ip()

    async def _wan_status(self) -> dict[str, object]:
        """Live WAN-mode status for the dashboard/API (cheap, every 15 s tick).

        ``topology`` is the EFFECTIVE value — what the running engine actually
        is (config.yaml, or the DB override the dashboard persisted). It only
        changes when the gateway restarts, so right after a panel apply it is
        still the OLD value. ``configured`` is the DESIRED value — what the box
        will boot into: the DB setting when the dashboard owns the topology,
        else the effective value. The dashboard WAN toggle keys off
        ``configured``, so an apply keeps the switch ON across the restart
        instead of snapping it back off (the v19 flip-off bug). ``pending`` is
        the dashboard-configured value (takes effect on the next restart) when
        the dashboard owns the topology, else None. ``source`` is who owns it
        (``config`` | ``dashboard``). The PPP fields show the ppp0 link state —
        in LAN mode they are always "n/a" (no ppp0). ``internet`` is a live
        bool (the WAN-tab green dot) — raw-IP TCP reachability probed every
        15 s tick so a dial failure or a dead line shows red immediately.
        """
        source = await self.database.get_setting("topology_source", "config")
        effective = getattr(self.cfg.engine, "topology", "lan") or "lan"
        configured = effective
        if source == "dashboard":
            db_topology = await self.database.get_setting("topology", "lan")
            configured = db_topology if db_topology in ("lan", "wan") else "lan"
        out: dict[str, object] = {
            "topology": effective, "configured": configured, "source": source,
            "pending": configured if source == "dashboard" else None,
            "ppp0": "n/a", "ppp_ip": "", "ppp_peer": "",
        }
        # The WAN-tab auto-renew schedule + last renewal time (ISO or ""). The
        # dashboard reads these from the WS snapshot / GET /api/wan so the
        # toggle + "last renewed" line stay live without a separate query.
        # Only fetched in WAN-ish modes: on a LAN box the setting is irrelevant
        # (it can't dial), and skipping the DB read every 15 s keeps the tick
        # light. Defaults for LAN so the payload always carries the keys.
        if effective == "wan" or configured == "wan":
            renew = await self.service.get_wan_renew_config()
            out["renew_enabled"] = renew["enabled"]
            out["renew_minutes"] = renew["minutes"]
            out["renew_last"] = renew["last"]
        else:
            out["renew_enabled"] = False
            out["renew_minutes"] = 15
            out["renew_last"] = ""
        if effective == "wan":
            # detect_ppp runs `ip` subprocesses — off the event loop.
            ppp = await asyncio.to_thread(detect_ppp, "ppp0")
            out["ppp0"] = ppp["state"]
            out["ppp_ip"] = ppp["local"]
            out["ppp_peer"] = ppp["peer"]
        # Internet-reachability probe — the WAN tab's green dot. In WAN mode the
        # router is a bridge so its LED is gone; this is the box's own indicator,
        # refreshed every maintenance tick (15 s). Run in a thread: a dead line
        # can hold the connect for up to `timeout` seconds and must never block
        # the event loop.
        # When the box's own internet is deliberately cut (the Gateway user is
        # blocked -> `gw_blocked` carries 0.0.0.0/0), the TCP egress probe is
        # dropped at the kernel and would read "down" even though the line and
        # every client are fine. DNS (UDP 53) is exempted from that block, so we
        # switch to a DNS probe: it proves the LINE delivers internet, and the
        # box's cut stays surfaced on the Gateway card instead of a false red.
        gateway_blocked = bool(getattr(self.engine, "gateway_blocked", False))
        try:
            if gateway_blocked:
                reachable = await asyncio.to_thread(self.dns_probe)
            else:
                reachable = await asyncio.to_thread(self.internet_probe)
        except Exception:  # noqa: BLE001 — a probe failure must not kill the tick
            log.exception("internet probe failed")
            reachable = False
        # In WAN mode ppp0 IS the internet path. A down dial means the gateway is
        # not serving internet even if the box itself still reaches the internet
        # through a leftover LAN route (e.g. the router not bridged yet) — the
        # dot must never claim a path the clients can't use. Gate it on the link
        # so "ppp0 down" and "internet ● Online" can't coexist.
        out["internet"] = reachable and (effective != "wan" or out["ppp0"] == "up")
        return out

    # ------------------------------------------------------------- signals

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, self._request_stop)
            except NotImplementedError:
                pass  # defensive: add_signal_handler unavailable in some runtimes

    def _request_stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------- shutdown

    async def shutdown(self) -> None:
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
        if self.engine is not None:
            self.engine.stop()
        if self.firewall is not None:
            # Cancel watchdogs; kernel rules are left in place (conservative).
            await self.firewall.shutdown()
        if self.shaper is not None:
            self.shaper.stop()  # leaves tc rules in place (conservative)
        if self.arp_lock is not None:
            self.arp_lock.stop()  # responder thread exits; deny rules stay
        if self.dnslog is not None:
            # Persist the read cursor so a restart resumes without re-reading
            # (and never double-counts the pre-shutdown tail), then stop the
            # thread. The tailer may be mid-poll; stop() joins it.
            try:
                await self.database.set_setting(
                    "dnslog_state", json.dumps(self.dnslog.state_snapshot()))
            except Exception:  # noqa: BLE001
                log.exception("dnslog: failed to persist read cursor")
            self.dnslog.stop()
        if self.wifi_probe is not None:
            # Stops the thread + tears down monitor mode (airmon-ng stop).
            self.wifi_probe.stop()
        await self.database.close()
        log.info("shutdown complete")


def main() -> None:
    args = _parse_args()
    resolved_config_path = cfg_mod.resolve_config_path(args.config)
    cfg = cfg_mod.load_config(resolved_config_path)
    if args.port is not None:
        cfg.web.port = args.port
    if args.debug:
        cfg.log_level = "DEBUG"

    setup_logging(cfg.log_level, cfg.log_file)
    log.info("Quota Manager starting (bundle %.1f GB, reset day %d)",
             cfg.bundle.total_gb, cfg.bundle.reset_day)

    gateway = Gateway(cfg, config_path=resolved_config_path)

    async def _serve() -> None:
        await gateway.startup()
        # TLS fallback: enforce-https persists cert paths in BOTH config.yaml
        # AND the DB.  When the user copies the project folder to the box,
        # config.yaml gets overwritten (wiping the TLS keys) but the DB and
        # the cert files on disk survive.  Apply DB-stored paths as a
        # fallback so HTTPS doesn't silently revert.
        if not cfg.web.tls_certfile:
            db_cert = await gateway.database.get_setting("tls_certfile")
            db_key = await gateway.database.get_setting("tls_keyfile")
            if db_cert and db_key:
                from pathlib import Path
                if Path(db_cert).is_file() and Path(db_key).is_file():
                    cfg.web.tls_certfile = db_cert
                    cfg.web.tls_keyfile = db_key
                    cfg.web.secure_cookies = True
                    log.info("TLS restored from DB: cert=%s key=%s",
                             db_cert, db_key)
        # Build the app AFTER startup: the WAN tab's topology manager is created
        # in startup() and the /api/wan endpoint must close over the real one.
        # The on-demand report gate needs the CLIENT subnet; the engine resolved
        # it from config at startup (explicit engine.client_subnet, else derived
        # from the dhcp block), so reuse its answer rather than re-derive.
        report_cfg = cfg.report
        client_net = getattr(getattr(gateway, "engine", None), "_client_net", "") or ""
        if client_net:
            report_cfg.client_subnet = client_net
        # WAF local-management exemption: feed the engine's resolved LAN
        # subnets into the WAF config so the panel is never blocked from the
        # local network (even in strict/WAN mode).
        local_nets = getattr(
            getattr(gateway, "engine", None), "_local_networks", None)
        if local_nets:
            # Dedupe against the loopback defaults already in the config.
            seen = set(cfg.waf.local_subnets)
            for net in local_nets:
                if net not in seen:
                    cfg.waf.local_subnets.append(net)
                    seen.add(net)
        app = create_app(gateway.database, gateway.service, gateway.holder,
                         log_path=cfg.log_file,
                         topology_manager=gateway.topology_manager,
                         shaping_sync=gateway._reshaping_now,
                         report_config=report_cfg,
                         dns_apply=gateway._apply_dns_now,
                         vpn_apply=gateway._apply_vpn_now,
                         vpn_status_getter=gateway._vpn_status,
                         wan_renew=gateway._renew_wan_ip,
                         shaping_state_getter=gateway._shaping_state,
                         stop_new_sync=gateway._apply_stop_new_now,
                         decline_random_sync=gateway._apply_decline_random_now,
                         active_ips_getter=gateway._latency_active_ips,
                         firewall=gateway.firewall,
                         firewall_wan_preapply=gateway._firewall_wan_preapply,
                         updater=gateway.updater,
                         web_config=cfg.web,
                         waf_config=cfg.waf)
        # TLS (web.tls_certfile / web.tls_keyfile): uvicorn terminates HTTPS
        # itself — a self-signed cert is fine (one-time trust step in the
        # browser). When TLS is on, secure_cookies is forced True by the app.
        server_config = uvicorn.Config(
            app,
            host=cfg.web.host,
            port=cfg.web.port,
            log_level="warning",
            ssl_certfile=cfg.web.tls_certfile or None,
            ssl_keyfile=cfg.web.tls_keyfile or None,
        )
        server = uvicorn.Server(server_config)
        try:
            await server.serve()
        finally:
            await gateway.shutdown()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
