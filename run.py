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
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Optional

import uvicorn

from api.app import create_app
from core import config as cfg_mod
from core.logging_setup import setup_logging
from quota import db as _db
from quota import dns_rules as _dns_rules
from quota.arp_scan import ArpScanner
from quota.dnslog import DnslogTailer
from quota.engine import GATEWAY_MAC, EngineSnapshot, SnapshotHolder
from quota.flows import (FlowSampler, enable_conntrack_accounting,
                         seed_dns_names_from_log)
from quota.firewall import FirewallManager
from quota.latency_probe import ArpRttProbe
from quota.netmgr import TopologyManager
from quota.nftables import NftablesEngine
from quota.tailscale import TailscaleManager
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


#: neighbor states that prove a client answered recently (see
#: ``Gateway._collect_neigh_active`` — STALE/PERMANENT deliberately excluded).
_LEASE_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
_NEIGH_PRESENT_STATES = frozenset({"REACHABLE"})
#: Max IPv6 addresses tracked (counted + cut) per device. SLAAC privacy
#: extensions rotate addresses; a hostile client could also spray random source
#: addresses — each tracked address costs two nft counters + rules, so the
#: number is capped (newest-seen wins is not knowable here; ULA first, stable).
IPV6_MAX_PER_MAC = 8


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
        self._last_wan_renew = time.monotonic()
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
        #: True once ``ip neigh`` was read successfully at least once — only
        #: then is the neighbor table a trustworthy presence source.
        self._neigh_collected = False
        #: IPv6 ISP probe + (ULA/global IPv6 address -> MAC) from `ip -6 neigh`
        self.ipv6_probe: object | None = None
        self._ipv6_neigh: dict[str, str] = {}
        #: MACs seen using a router-assigned GLOBAL IPv6 address (they route
        #: around this box — uncounted and uncuttable until the router's RA is off)
        self._ipv6_direct_macs: set[str] = set()
        #: consecutive ARP sweeps that found nobody (see _maybe_latency_tick).
        self._empty_sweeps = 0
        #: monotonic time of the previous counter flush (live-speed window).
        self._last_flush_mono: float | None = None
        #: set when _persist_lease creates a new device — _sync_dnsmasq_leases
        #: resets _last_latency_sweep so the ARP probe runs in the same tick,
        #: turning the new device's LED blue immediately instead of waiting up
        #: to a full sweep interval (30 s).
        self._new_devices_detected = False
        #: per-destination traffic (conntrack byte deltas) for the Live-traffic view
        _hist = getattr(cfg, "history", None)
        self.flows = FlowSampler(
            owner_lookup=bool(getattr(_hist, "ip_owner_lookup", True)),
            owner_cache=Path(cfg.db_path).parent / "ip-owners.json")

    # ------------------------------------------------------------- startup

    async def startup_core(self) -> None:
        """Connect DB, ensure period, instantiate managers.

        Runs in < 20 ms so the web server can bind port 8080 and serve the UI
        immediately without waiting for kernel rules or network commands.
        """
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

        if self.cfg.engine.enabled and self.engine is None:
            self.engine = _make_engine(self.cfg, self.holder)
        if self.shaper is None:
            self.shaper = _make_shaper(self.cfg)
        if self.dns_manager is None:
            self.dns_manager = _make_dns_manager(self.cfg)
        if self.vpn_manager is None:
            self.vpn_manager = _make_vpn_manager(self.cfg)
        if self.tun2socks_manager is None:
            self.tun2socks_manager = _make_tun2socks_manager(self.cfg)
        if self.firewall is None and getattr(self.cfg, "firewall", None) is not None \
                and self.cfg.firewall.enabled:
            self.firewall = FirewallManager(self.cfg, self.database,
                                            web_port=self.cfg.web.port)
        if self.arp_scanner is None:
            self.arp_scanner = ArpScanner(self.cfg)
        self._install_signal_handlers()

    async def startup_subsystems(self) -> None:
        """Background initialization of kernel rules, shaping, firewall, tailers."""
        # -- startup self-heal (infrastructure resilience) -----------------
        try:
            await asyncio.to_thread(self._startup_self_heal)
        except Exception as e:
            log.warning("startup self-heal notice: %s", e)

        # -- packet engine (nftables kernel rules) --------------------------
        if self.engine is not None and getattr(self.engine, "available", False):
            try:
                await asyncio.to_thread(self.engine.start)
                log.info("packet engine started (%s)", getattr(self.engine, "name", "?"))
                self._net_watch = True      # arm the per-tick network self-heal
            except Exception as e:
                log.warning("packet engine start notice: %s", e)

        # -- speed shaping (tc) ---------------------------------------------
        if self.shaper is not None:
            try:
                await asyncio.to_thread(self.shaper.start)
            except Exception as e:
                log.warning("shaper start notice: %s", e)

        # -- VPN share (policy routing into the box's tunnel) ----------------
        try:
            await self._sync_vpn_share()
        except Exception as e:
            log.warning("vpn share sync notice: %s", e)

        # -- kernel firewall (nftables quota_firewall) ------------------------
        if self.firewall is not None:
            try:
                await self.firewall.load_config()
                await self.firewall.load_geo()
                await asyncio.to_thread(self.firewall.apply)
                log.info("firewall started (%s mode)",
                         "wan" if self.firewall.topology == "wan" else "lan")
            except Exception as e:
                log.warning("firewall start notice: %s", e)

        # -- ARP gateway-lock (opt-in) ---------------------------------------
        if (getattr(self.cfg.engine, "gateway_arp_lock", False)
                and getattr(self.cfg.engine, "topology", "lan") != "wan"
                and self.arp_lock is None):
            try:
                from quota.arp_lock import ArpLock
                self.arp_lock = ArpLock(self.cfg)
                self.arp_lock.start()
            except Exception as e:
                log.warning("arp lock start notice: %s", e)

        # -- DNS browsing history (quota.dnslog) ------------------------------
        if getattr(self.cfg, "history", None) is not None and self.cfg.history.enabled and self.dnslog is None:
            try:
                resume: dict[str, object] = {}
                try:
                    resume = json.loads(
                        await self.database.get_setting("dnslog_state", "{}") or "{}")
                except ValueError:
                    resume = {}
                self.dnslog = DnslogTailer(self.cfg.history.dnsmasq_log_file,
                                           resume=resume)
                self.dnslog.start()
                log.info("DNS-history tailer started (%s, resume=%s)",
                         self.cfg.history.dnsmasq_log_file,
                         "yes" if resume else "no")
            except Exception as e:
                log.warning("dnslog start notice: %s", e)

        # -- passive WiFi probe (quota.wifi_probe) -----------------------
        wprobe_cfg = getattr(getattr(self.cfg, "network", None), "wifi_probe", None)
        if self.wifi_probe is None and wprobe_cfg is not None \
                and getattr(wprobe_cfg, "enabled", False):
            try:
                from quota.wifi_probe import WifiProbe
                self.wifi_probe = WifiProbe(
                    interface=getattr(wprobe_cfg, "interface", "") or "",
                    poll_interval=float(
                        getattr(wprobe_cfg, "poll_interval", 5.0) or 5.0),
                    sighted_ttl=float(
                        getattr(wprobe_cfg, "sighted_ttl", 600.0) or 600.0))
            except Exception as e:
                log.warning("wifi probe init notice: %s", e)

        if self.wifi_probe is not None:
            try:
                self.wifi_probe.start()
                log.info("WiFi probe started (interface %r)",
                         getattr(wprobe_cfg, "interface", "") or "auto")
            except Exception as e:
                log.warning("wifi probe start notice: %s", e)

        # -- ISP IPv6 probe (quota.ipv6_probe) ------------------------------
        dhcp_cfg = getattr(self.cfg, "dhcp", None)
        if self.ipv6_probe is None and getattr(dhcp_cfg, "ipv6_enable", True):
            try:
                from quota.ipv6_probe import Ipv6Probe

                async def _on_ipv6_change(active: bool, details: dict[str, Any]) -> None:
                    _set6 = getattr(self.engine, "set_ipv6_wan", None)
                    if _set6 is not None:
                        await asyncio.to_thread(_set6, active)
                    await self.database.add_event(
                        "IPv6: ISP IPv6 is now "
                        + ("online — dual-stack active" if active
                           else "offline — clients use IPv4"),
                        "info" if active else "warn")

                self.ipv6_probe = Ipv6Probe(
                    cfg=self.cfg,
                    check_interval_sec=float(
                        getattr(dhcp_cfg, "ipv6_probe_interval_s", 600.0) or 600.0),
                    on_status_change=_on_ipv6_change)
                await self.ipv6_probe.start()
            except Exception as e:  # noqa: BLE001
                log.warning("ipv6 probe start notice: %s", e)

        log.info("DHCP + DNS via dnsmasq on the client subnet — Python "
                 "DHCP/DNS/ARP stack not used")

        # -- maintenance loop -----------------------------------------------
        if self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def startup(self) -> None:
        """Connect DB, ensure period, start optional subsystems."""
        await self.startup_core()
        await self.startup_subsystems()

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

    def _net_problems(self) -> list[str]:
        """Cheap check of the kernel pieces the gateway relies on but a package
        install / service restart elsewhere can silently remove: forwarding,
        the masquerade NAT table, and the packet engine's table."""
        from quota import startup_health as sh
        if not shutil.which("nft"):
            return []
        problems: list[str] = []
        try:
            if sh._PROC_IPFORWARD.read_text(encoding="utf-8").strip() == "0":
                problems.append("ip_forward")
        except OSError:
            pass
        run = sh._default_run
        if not (sh._table_exists(run) and sh._has_masquerade(run)):
            problems.append("nat")
        eng = self.engine
        if eng is not None and getattr(eng, "available", False):
            code, _ = run(["nft", "list", "set", "inet", eng.table, "blocked"])
            if code != 0:
                problems.append("engine")
        return problems

    def _network_heal(self, reason: str = "") -> list[str]:
        """Repair whatever :meth:`_net_problems` finds (idempotent, never raises).

        Installing a package (Tailscale) or bringing up a tunnel can reset
        forwarding / wipe nft tables; clients then lose internet until the
        service restarts. This restores them in place. Returns what it fixed.
        """
        try:
            eng = self.engine
            if eng is None or not getattr(eng, "available", False):
                return []          # no kernel rules in play (tests, non-Linux)
            if reason == "tick" and not getattr(self, "_net_watch", False):
                return []          # only after the real startup has built the rules
            now = time.monotonic()
            if reason == "tick" and now - getattr(self, "_last_heal_mono", -999.0) < 60.0:
                return []          # back off after a repair attempt
            problems = self._net_problems()
            if not problems:
                return []
            self._last_heal_mono = now
            log.warning("network self-heal (%s): restoring %s", reason or "tick", ", ".join(problems))
            self._startup_self_heal()
            if "engine" in problems and self.engine is not None:
                self.engine.start()
            return problems
        except Exception:  # noqa: BLE001
            log.exception("network self-heal failed")
            return []

    def _startup_self_heal(self) -> None:
        """Verify / recreate infrastructure the app depends on but doesn't own.

        The setup script creates ``quota_nat`` (masquerade NAT) and the NIC
        static IPs; either can be silently lost (``nft flush ruleset``,
        NM reconnection, broken ``/etc/nftables.conf`` symlink).  This runs
        once at startup before the engine / firewall are built.
        """
        from quota.startup_health import (  # lazy: subprocess + /proc
            ensure_nat_table, ensure_network_infrastructure)

        # -- conntrack byte counters (Live traffic: where the bytes go) -----
        enable_conntrack_accounting()
        try:   # name connections that were already open before this start
            seed_dns_names_from_log(self.cfg.history.dnsmasq_log_file)
        except Exception:  # noqa: BLE001 — cosmetic only
            log.debug("dns name seeding skipped", exc_info=True)

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
            ensure_nat_table(
                client_subnet,
                ipv6_prefix=(getattr(self.cfg.dhcp, "ipv6_prefix", "")
                             if getattr(self.cfg.dhcp, "ipv6_enable", False) else ""))
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
            ipv6_gateway=(getattr(self.cfg.dhcp, "ipv6_gateway", "")
                          if getattr(self.cfg.dhcp, "ipv6_enable", False) else ""),
        )

        # -- IPv6 host config (dnsmasq RA + sysctl) -------------------------
        # Applied by the app itself so an update never needs a manual
        # re-run of the setup script.
        if not getattr(self.cfg.dhcp, "ipv6_enable", False):
            # Off switch: undo our own fragments (no-op if never enabled).
            try:
                from quota.ipv6_setup import (disable_ipv6_host_config,
                                              ensure_box_ignores_ula_dns)
                from quota.startup_health import _find_lan_interface, _SYS_NET, _default_run
                ensure_box_ignores_ula_dns(
                    prefix=getattr(self.cfg.dhcp, "ipv6_prefix", "") or "fd00:da7a:2::/64",
                    iface=_find_lan_interface(_default_run, _SYS_NET) or "",
                    fallback_dns=list(getattr(self.cfg.dhcp, "dns_servers", []) or []))
                if disable_ipv6_host_config()["dnsmasq_changed"]:
                    _reload_dnsmasq()
            except Exception:  # noqa: BLE001
                log.exception("startup self-heal: IPv6 disable failed")
        else:
            try:
                from quota.ipv6_setup import ensure_ipv6_host_config
                from quota.startup_health import _find_lan_interface, _SYS_NET, _default_run
                res = ensure_ipv6_host_config(
                    prefix=getattr(self.cfg.dhcp, "ipv6_prefix", "") or "fd00:da7a:2::/64",
                    gateway=getattr(self.cfg.dhcp, "ipv6_gateway", "") or "fd00:da7a:2::1",
                    iface=_find_lan_interface(_default_run, _SYS_NET) or "")
                from quota.ipv6_setup import ensure_box_ignores_ula_dns
                ensure_box_ignores_ula_dns(
                    prefix=getattr(self.cfg.dhcp, "ipv6_prefix", "") or "fd00:da7a:2::/64",
                    iface=_find_lan_interface(_default_run, _SYS_NET) or "",
                    fallback_dns=list(getattr(self.cfg.dhcp, "dns_servers", []) or []))
                if res["dnsmasq_changed"]:
                    _reload_dnsmasq()
                from quota.ipv6_setup import rollback_if_dnsmasq_down
                rollback_if_dnsmasq_down()
            except Exception:  # noqa: BLE001 — never block startup on IPv6 setup
                log.exception("startup self-heal: IPv6 host config failed")

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
                if self.service.is_temporarily_kicked(mac):
                    log.info("kicked MAC %s is in 5s cooldown — not "
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
                    # Guest mode is active: new guest devices are registered in the
                    # pending lock (0 GB allowance, admin-cut) until admin approves.
                    dev = await self.database.upsert_device(
                        mac, name="", quota_mode=_db.QUOTA_FIXED,
                        fixed_gb=0.0, guest=True)
                    await self.database.set_device_state(
                        dev.id, _db.BLOCK_ADMIN)
                    if dev.user_id:
                        await self.database.update_user(
                            dev.user_id, block_state=_db.BLOCK_ADMIN)
                    await self.database.add_event(
                        f"New GUEST on network: {dev.mac} ({ip}) — "
                        f"awaiting admin approval", "info", dev.id)
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

    async def _purge_phantom_devices(self) -> None:
        """Delete devices/leases whose "MAC" is not a MAC (once per run).

        An earlier version registered DHCPv6 IAIDs ("0", "415503221") as devices.
        Their pending-guest block was then pushed to nftables as a MAC, failed
        with "Invalid LL address" and switched the packet engine off.
        """
        if getattr(self, "_phantoms_purged", False):
            return
        self._phantoms_purged = True
        try:
            for dev in await self.database.list_devices():
                if dev.mac != GATEWAY_MAC and not _LEASE_MAC_RE.match(dev.mac or ""):
                    await self.database.delete_device(dev.id)
                    await self.database.delete_lease(dev.mac)
                    log.warning("removed phantom device %r (not a MAC address)", dev.mac)
            for lease in await self.database.list_leases():
                if not _LEASE_MAC_RE.match(lease.mac or ""):
                    await self.database.delete_lease(lease.mac)
        except Exception:  # noqa: BLE001
            log.exception("phantom device purge failed")

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
        await self._purge_phantom_devices()
        seen: set[str] = set()
        for line in text.splitlines():
            parts = line.split()
            # format: <expiry-epoch> <mac> <ip> [hostname] [clientid]
            if len(parts) < 3:
                continue
            mac, ip = parts[1], parts[2]
            # DHCPv6 leases look like `<expiry> <iaid> <ipv6> <host> <duid>`: the
            # 2nd field is an IAID (a number), NOT a MAC. Treating it as one
            # registered phantom "Guest" devices named "0" / "415503221".
            if ":" in ip or not _LEASE_MAC_RE.match(mac):
                continue
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
        """Read the kernel neighbor table: IPv4 presence + IPv6 address map.

        IPv4 — only a *fresh* (``REACHABLE``) entry proves presence into
        ``_neigh_active_ips``. ``STALE`` means "no confirmation for a while" and
        Linux keeps STALE entries for hours (the GC only runs under memory
        pressure), so a phone that left the house would otherwise keep a green
        LED all day. ``PERMANENT``/``NOARP`` are static and prove nothing. An
        idle-but-connected device is re-confirmed by the ARP probe sweep.

        IPv6 — every non-failed row with a link-layer address maps a client's
        ULA address (inside ``dhcp.ipv6_prefix``) to its MAC into
        ``_ipv6_neigh``. A STALE row is fine here: this is address -> MAC
        bookkeeping, not presence. Link-local rows are ignored (never reach the
        internet); global (2000::/3) rows mean the device took an address
        straight from the ISP router and bypasses this box — remembered in
        ``_ipv6_direct_macs`` so the dashboard can warn.

        Falls back to plain ``ip neigh`` text when ``-j`` is unavailable (old
        iproute2). A missing/unusable ``ip`` binary degrades to a no-op.
        """
        leases = await self.database.list_leases()
        wanted = {l.ip for l in leases} if leases else set()
        rows: list[tuple[str, str, set[str]]] = []  # (dst, lladdr, states)
        code, out = await asyncio.to_thread(
            self._ip_run, ["ip", "-j", "neigh"])
        if code == 0 and out:
            for row in _iter_json_neigh(out):
                st = row.get("state") or ""
                states = {x.upper() for x in (st if isinstance(st, list) else [st])
                          if isinstance(x, str)}
                rows.append((row.get("dst") or "",
                             (row.get("lladdr") or "").strip().lower(), states))
            ok = True
        else:
            ok = False
            code2, out2 = await asyncio.to_thread(
                self._ip_run, ["ip", "neigh"])
            if code2 == 0 and out2:
                ok = True
                for line in out2.splitlines():
                    fields = line.split()
                    if len(fields) < 2:
                        continue
                    lladdr = ""
                    if "lladdr" in fields:
                        i = fields.index("lladdr")
                        if i + 1 < len(fields):
                            lladdr = fields[i + 1].strip().lower()
                    rows.append((fields[0], lladdr, {f.upper() for f in fields[1:]}))
        if not ok:
            return

        from quota.ipv6_probe import is_global_unicast_ipv6
        try:
            ula_net = ipaddress.ip_network(
                getattr(self.cfg.dhcp, "ipv6_prefix", "") or "fd00:da7a:2::/64",
                strict=False)
        except ValueError:
            ula_net = ipaddress.ip_network("fd00:da7a:2::/64")
        active: set[str] = set()
        ipv6_neigh: dict[str, str] = {}
        direct: set[str] = set()
        for dst, lladdr, states in rows:
            if ":" not in dst:
                if dst in wanted and states & _NEIGH_PRESENT_STATES:
                    active.add(dst)
                continue
            if not lladdr or states & {"FAILED", "INCOMPLETE"}:
                continue
            try:
                addr = ipaddress.ip_address(dst.split("%")[0])
            except ValueError:
                continue
            if addr in ula_net:
                ipv6_neigh[dst.lower()] = lladdr
            elif is_global_unicast_ipv6(dst):
                direct.add(lladdr)
        self._neigh_collected = True
        self._neigh_active_ips = active
        self._ipv6_neigh = ipv6_neigh
        self._ipv6_direct_macs = direct

    def _ipv6_wan_wanted(self) -> bool:
        """Should client IPv6 be allowed out to the internet right now?

        Yes only when the ISP really delivers IPv6 AND the household is not
        being relayed through the VPN: VPN share policy-routes the IPv4 client
        subnet only, so open IPv6 would leak every device around the tunnel.
        """
        if not bool(getattr(self.ipv6_probe, "wan_ipv6_active", False)):
            return False
        return str(self._last_vpn_status.get("state", "")) != "on"

    def _device_ipv6_map(self) -> dict[str, str]:
        """MAC -> one display IPv6 address (the ULA address the box tracks)."""
        out: dict[str, str] = {}
        for ip6, mac in sorted(self._ipv6_neigh.items()):
            out.setdefault(mac, ip6)
        return out

    def _tracked_ipv6(self, known_macs: set[str]) -> dict[str, str]:
        """IPv6 address -> MAC for counting/cutting: only KNOWN devices'
        addresses, capped at ``IPV6_MAX_PER_MAC`` per device (deterministic:
        sorted, so the same addresses win every tick), plus static reservations."""
        per_mac: dict[str, list[str]] = {}
        for ip6, mac in sorted(self._ipv6_neigh.items()):
            if mac in known_macs:
                per_mac.setdefault(mac, []).append(ip6)
        out: dict[str, str] = {}
        for mac, addrs in per_mac.items():
            for ip6 in addrs[:IPV6_MAX_PER_MAC]:
                out[ip6] = mac
        return out

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
        # Every completed sweep refreshes the responder map: devices that
        # answered the ARP burst plus those with a FRESH kernel neighbor entry.
        # A single empty sweep keeps the previous map (the probe backend can
        # silently return {} — NIC resolution failure, sleepy phones missing
        # one burst), but a SECOND empty sweep in a row means nobody is home,
        # so the map is cleared and every LED goes grey. Without this the old
        # map aged out into "lease-based" mode and every leased device stayed
        # green for the whole lease time.
        self._latency_sweep_ran = True
        fresh = set(rtts) | self._neigh_active_ips
        if fresh:
            self._latency_responders = fresh
            self._empty_sweeps = 0
        else:
            self._empty_sweeps += 1
            if self._empty_sweeps >= 2:
                self._latency_responders = set()

    def _recent_traffic_ips(self) -> set[str]:
        """IPs that moved bytes in the latest counter flush — a device that is
        passing traffic right now is, by definition, connected."""
        try:
            by_ip = self.holder.get().by_ip
        except Exception:  # noqa: BLE001
            return set()
        return {ip for ip, c in by_ip.items()
                if getattr(c, "up", 0) or getattr(c, "down", 0)}

    def _latency_active_ips(self) -> Optional[set[str]]:
        """IPs that are connected NOW, or None when there is no presence
        signal at all (caller falls back to lease-based connectedness).

        Sources, strongest first: the latest ARP sweep, fresh kernel neighbor
        entries, and IPs that carried traffic in the last flush. When the ARP
        probe is off or its data went stale, the neighbor table + traffic are
        used on their own (never the bare lease, which lags reality by up to
        ``LEASE_HOURS`` and kept away devices green)."""
        traffic = self._recent_traffic_ips()
        probe_fresh = (
            self.latency_probe is not None and self._latency_sweep_ran
            and not (self._latency_interval > 0
                     and time.monotonic() - self._last_latency_sweep
                     > 3 * self._latency_interval))
        if probe_fresh:
            return set(self._latency_responders) | traffic
        if self._neigh_collected:
            return set(self._neigh_active_ips) | traffic
        return None

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
                mac6 = self._ipv6_neigh.get(ev.ip.lower()) if ":" in ev.ip else None
                if mac6:
                    dev = await self.database.get_device(mac=mac6)
                else:
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

    async def _timed(self, name: str, coro: Any) -> Any:
        """Await one tick step and name it in the log when it is slow, so a
        53 s tick can be attributed to the step that caused it."""
        t0 = time.perf_counter()
        try:
            return await coro
        finally:
            dt = time.perf_counter() - t0
            if dt > 1.0:
                log.warning("maintenance step %s took %.1fs", name, dt)

    async def _maintenance_tick(self) -> None:
        # 1. Roll the quota period if stale (covers month boundary).
        await self.service.ensure_period()

        # 1b. Learn device bindings from dnsmasq's lease file.
        await self._timed("_sync_dnsmasq_leases", self._sync_dnsmasq_leases())

        # 1b2. Learn each device's source NIC (NIC tag) from the kernel
        #      neighbor table. Only updates when a fresh value is seen, so an
        #      offline device keeps its last-known interface.
        await self._timed("_collect_neigh_active", self._collect_neigh_active())

        # 1b3. Passive WiFi probe: collects monitor-mode snapshots for the
        #      yield check in _maybe_latency_tick (when the monitor probe is
        #      available, the ARP-RTT probe yields).
        await self._timed("_wifi_probe_tick", self._wifi_probe_tick())

        # 1b4. ARP-RTT probe (any hardware). Slow cadence,
        #      off the event loop (raw sockets + sleeps in a worker thread).
        await self._timed("_maybe_latency_tick", self._maybe_latency_tick())

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
        await self._timed("_wan_ip_renew_tick", self._wan_ip_renew_tick())

        # 1h. GitHub self-update check (24 h gate lives inside the updater —
        #     maybe_check is a no-op until the interval elapses). A failure is
        #     swallowed inside; the state surfaces in the Admin tab. None when
        #     cfg.updates.enabled=false (master switch / hermetic tests).
        if self.updater is not None:
            try:
                await self._timed("updater.maybe_check", self.updater.maybe_check())
            except Exception:  # noqa: BLE001 — never let the updater stall a tick
                log.exception("update check tick failed")

        # 2. Drain the packet engine's counters into usage_daily.
        #    flush() shells out to `nft -j list counters` (a subprocess). Run
        #    it off the event loop so a slow nft never stalls the WebSocket
        #    push or the API.
        live_by_ip: dict[str, object] = {}
        live_lan_by_ip: dict[str, object] = {}
        flush_interval = 15.0
        if self.engine is not None:
            snap = await asyncio.to_thread(self.engine.flush)
            _mono = time.monotonic()
            if self._last_flush_mono is not None:
                flush_interval = min(120.0, max(1.0, _mono - self._last_flush_mono))
            self._last_flush_mono = _mono
            live_by_ip = snap.by_ip
            live_lan_by_ip = getattr(snap, "lan_by_ip", None) or {}
            today = _dt.date.today().isoformat()
            if snap.by_ip or snap.gateway.up or snap.gateway.down:
                devices = await self.database.list_devices()
                devices_by_mac = {d.mac.lower(): d for d in devices}
                usage_records: list[tuple[int, str, int, int]] = []

                if snap.by_ip:
                    for ip, counters in snap.by_ip.items():
                        if counters.up == 0 and counters.down == 0:
                            continue
                        mac = snap.ip_to_mac.get(ip)
                        if not mac:
                            mac = await self.database.get_mac_for_ip(ip)
                        if not mac:
                            continue  # IP not tied to a known device
                        dev = devices_by_mac.get(mac.lower())
                        if dev is None:
                            continue
                        usage_records.append((dev.id, today, counters.up, counters.down))

                # The box's OWN internet (input/output hooks, q_gw_* counters):
                # charged to the protected "Gateway" user's device so the machine's
                # bundle consumption sits inside the quota math. The box's MAC has
                # no lease, so it never appears in snap.by_ip — only in snap.gateway.
                if snap.gateway.up or snap.gateway.down:
                    is_gw_blocked = bool(getattr(self.engine, "gateway_blocked", False))
                    if not is_gw_blocked:
                        box = devices_by_mac.get(GATEWAY_MAC.lower())
                        if box is not None:
                            usage_records.append((box.id, today, snap.gateway.up, snap.gateway.down))

                if usage_records:
                    await self.database.add_usage_batch(usage_records)

            # LOCAL (LAN) traffic: stored apart from the metered usage, never
            # part of quota math - purely for the dashboard's LAN monitor.
            lan_by_ip = getattr(snap, "lan_by_ip", None) or {}
            if lan_by_ip:
                try:
                    devices_l = await self.database.list_devices()
                    by_mac_l = {d.mac.lower(): d for d in devices_l}
                    lan_records: list[tuple[int, str, int, int]] = []
                    for ip, c in lan_by_ip.items():
                        mac = snap.ip_to_mac.get(ip) or await self.database.get_mac_for_ip(ip)
                        dev = by_mac_l.get(mac.lower()) if mac else None
                        if dev is not None and (c.up or c.down):
                            lan_records.append((dev.id, today, c.up, c.down))
                    await self.database.add_lan_usage_batch(lan_records)
                except Exception:  # noqa: BLE001 - monitoring must never stall the tick
                    log.exception("LAN usage drain failed")

        # 3. Recompute block states from usage vs allowances.
        changes = await self.service.evaluate_blocks()
        for ch in changes:
            log.warning("device %s (%s) -> %s", ch["device_id"], ch["mac"], ch["state"])

        # 4. Push fresh enforcement maps into the engine + holder.
        state = await self.service.snapshot_state()
        ip_to_mac = {v["ip"]: mac for mac, v in state.items() if v.get("ip")}
        # IPv6: the engine counts/cuts each known device's tracked IPv6
        # addresses exactly like its IPv4 lease (static reservations + the
        # ULA addresses learned from the neighbor table).
        ip_to_mac_all = dict(ip_to_mac)
        for s_row in await self.database.list_static_leases():
            ip6 = (s_row.get("ip6") or "").strip().lower()
            if ip6 and s_row["mac"].lower() in state:
                ip_to_mac_all[ip6] = s_row["mac"].lower()
        ip_to_mac_all.update(self._tracked_ipv6({m.lower() for m in state}))
        self._last_ip_to_mac = ip_to_mac_all  # for an immediate shaping re-sync
        blocked = {mac: v["blocked"] for mac, v in state.items()}
        pending_guests = {
            v["ip"] for mac, v in state.items()
            if v.get("ip") and v.get("guest") and v.get("blocked")
        }
        engine_available = False
        gateway_blocked = None
        if self.engine is not None:
            # nft update_state + set_gateway_blocked shell out to `nft`
            # (a subprocess per rebuild, ~80 on a first boot). Run them off
            # the event loop so a slow kernel sync never stalls the WS push.
            try:
                await asyncio.to_thread(
                    self.engine.update_state, ip_to_mac_all, blocked, list(pending_guests))
            except TypeError:
                await asyncio.to_thread(
                    self.engine.update_state, ip_to_mac_all, blocked)
            # The box's own internet is cut via the gateway chains (gw_blocked
            # set) — its MAC has no lease/IP, so it never enters the forward
            # blocked set above. The Gateway user's resolved state drives the
            # toggle; the engine cache-gates a no-op on an unchanged state.
            _set6 = getattr(self.engine, "set_ipv6_wan", None)
            if self.ipv6_probe is not None and _set6 is not None:
                await asyncio.to_thread(_set6, self._ipv6_wan_wanted())
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
        # Where is each device's traffic going? Diff conntrack byte counters
        # (off-loop: /proc read + parse). Best effort — never blocks the tick.
        flows_by_mac: dict[str, list[dict]] = {}
        try:
            _addr_mac = {a: m for a, m in ip_to_mac_all.items()}
            await asyncio.to_thread(self.flows.sample, set(_addr_mac))
            by_mac_addrs: dict[str, set[str]] = {}
            for a, m in _addr_mac.items():
                by_mac_addrs.setdefault(m, set()).add(a)
            for m, addrs in by_mac_addrs.items():
                dests = self.flows.top_destinations(addrs)
                if dests:
                    flows_by_mac[m] = dests
        except Exception:  # noqa: BLE001
            log.debug("flow sampling failed", exc_info=True)
        # Live counters = the flushed delta, so the dashboard shows real
        # recent traffic instead of always-zero values.
        self.holder.swap(EngineSnapshot(
            by_ip=live_by_ip,
            lan_by_ip=live_lan_by_ip,
            ip_to_mac=ip_to_mac_all, blocked=blocked, rogue=list(self._rogues),
            wan_status=await self._wan_status(),
            gateway_blocked=gateway_blocked,
            engine_available=engine_available,
            pending_guests=pending_guests,
            interval_s=flush_interval,
            flows_by_mac=flows_by_mac,
            ts=time.time()))

        # 5. Linux: reconcile the tc speed-shaping tree (settings + live IPs).
        #    update_state is signature-gated, so an unchanged state does not
        #    touch the kernel; a DB edit lands here within one tick (<=15 s).
        await self._timed("_sync_shaping", self._sync_shaping(ip_to_mac_all))

        # 6. Regenerate dnsmasq's domain-rule + tag config if anything
        #    changed (new device -> new tag, or a rule/preset/DNS-server
        #    edit that arrived without going through the API's immediate
        #    apply — e.g. a test or a future non-HTTP caller).
        await self._timed("_sync_dns_rules", self._sync_dns_rules())

        # 6b. Reconcile the DHCP-refusal fragment for the STOP-NEW + Decline-random
        #     gates (cheap content diff; self-heals a gate toggle that missed
        #     the API's immediate apply, a deleted fragment, or a leftover
        #     fragment after a gate was turned off).
        await self._timed("_sync_refuse_fragment", self._sync_refuse_fragment())

        # 7. Reconcile the VPN-share policy routing (cheap when nothing
        #    changed; re-applies when the VPN client restarts/re-creates
        #    its tunnel, and removes the rule if the tunnel vanished so
        #    clients never ride a dead VPN into a blackhole).
        await self._timed("_sync_vpn_share", self._sync_vpn_share())

        # 8. Reconcile the kernel firewall (quota_firewall). Re-applies ONLY
        #    when the stored config or the topology changed (signature-gated),
        #    drains the scan-watch + fw_* counters into the Firewall log view,
        #    and persists any ban/drop events. All nft work runs off the loop.
        if self.firewall is not None \
                and getattr(self.firewall, "available", False):
            try:
                await self._timed("firewall.reconcile", self.firewall.reconcile())
            except Exception:  # noqa: BLE001 — never let the firewall stall a tick
                log.exception("firewall reconcile failed")

        # 9. Network self-heal: forwarding / NAT / engine table that something
        #    outside the app removed (a package install, an nftables restart).
        try:
            fixed = await asyncio.to_thread(self._network_heal, "tick")
            if fixed:
                await self.database.add_event(
                    "Network self-heal restored: " + ", ".join(fixed), "warn")
        except Exception:  # noqa: BLE001
            log.exception("network self-heal tick failed")

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
                devices = {d.mac.lower(): d for d in await self.database.list_devices()}
                rate_map: list[dict[str, object]] = []
                # IPv6 addresses ride on their device's (IPv4) entry as ``ip6``
                v6_by_mac: dict[str, list[str]] = {}
                for ip, mac in ip_to_mac.items():
                    if ":" in ip:
                        v6_by_mac.setdefault(mac.lower(), []).append(ip)
                for ip, mac in ip_to_mac.items():
                    if ":" in ip:
                        continue
                    dev = devices.get(mac.lower())
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
                        "ip6": sorted(v6_by_mac.get(mac.lower(), ())),
                    })
                # "VPN active" = the tunnel is genuinely carrying the client
                # subnet right now (VpnShareManager applied the policy route).
                # The DB switch alone is NOT enough: with the switch on but the
                # tunnel down, clients fall back to the direct uplink and must
                # not be shaped with tunnel margins.
                vpn_active = str(
                    self._last_vpn_status.get("state", "")) == "on"
                if not vpn_active:
                    for _vc in (getattr(self, "vpn_core", None),
                                getattr(self, "vpn_manager", None)):
                        if _vc is not None and str(
                                getattr(_vc, "state", "")) == "connected":
                            vpn_active = True
                            break

                shaping_enabled = config["enabled"]
                total_down = config["total_down_mbps"]
                total_up = config["total_up_mbps"]
                fair_sharing = False
                rates_measured = False

                if vpn_active:
                    shaping_enabled = True
                    fair_sharing = True
                    sc = self.cfg.shaping
                    # Panel values win; config.yaml is only the fallback.
                    vdown = float(config.get("vpn_down_mbps") or 0) or float(
                        getattr(sc, "vpn_down_mbps", 0) or 0)
                    vup = float(config.get("vpn_up_mbps") or 0) or float(
                        getattr(sc, "vpn_up_mbps", 0) or 0)
                    if vdown > 0 and vup > 0:
                        # Operator-measured through-VPN speeds replace the
                        # WAN totals (a half-set pair never engages).
                        total_down, total_up = vdown, vup
                        rates_measured = True
                    elif total_down <= 0 or total_up <= 0:
                        # Unset totals leave the tunnel UNSHAPED, which is the
                        # exact "one download freezes everyone" condition:
                        # engage a conservative baseline and say so loudly.
                        if not getattr(self, "_warned_vpn_rates", False):
                            self._warned_vpn_rates = True
                            log.warning(
                                "VPN share is active but the line speed is "
                                "not set (Network tab totals / shaping."
                                "vpn_down_mbps+vpn_up_mbps) — using a 30/10 "
                                "Mbps baseline. Run a speed test THROUGH the "
                                "VPN and set the real numbers.")
                        if total_down <= 0:
                            total_down = 30.0
                        if total_up <= 0:
                            total_up = 10.0

                # A tree rebuild runs ~70-115+ sequential `tc` subprocesses
                # (~1.5-5 s). Keep it OFF the event loop (WS pushes + API
                # requests starve during the rebuild otherwise). The lock
                # still serializes tick vs API-immediate re-syncs, and
                # update_state's signature gate makes the common no-op free.
                try:
                    await asyncio.to_thread(
                        shaper.update_state,
                        rate_map, shaping_enabled,
                        total_down,
                        total_up,
                        lan_rate_mbps=config["lan_rate_mbps"],
                        fair_sharing=fair_sharing,
                        vpn_active=vpn_active,
                        rates_measured=rates_measured,
                        vpn_pct=float(config.get("vpn_pct") or 0))
                except TypeError:
                    # Older/narrower update_state signatures: drop the newer
                    # kwargs progressively instead of losing the shaping call.
                    try:
                        await asyncio.to_thread(
                            shaper.update_state,
                            rate_map, shaping_enabled,
                            total_down,
                            total_up,
                            lan_rate_mbps=config["lan_rate_mbps"],
                            fair_sharing=fair_sharing)
                    except TypeError:
                        await asyncio.to_thread(
                            shaper.update_state,
                            rate_map, shaping_enabled,
                            total_down,
                            total_up,
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
                # When the adult content preset is enabled, route DNS through
                # Cloudflare Family DNS (1.1.1.3 / 1.0.0.3) for live adult blocking
                # and mandatory SafeSearch enforcement across all search engines.
                porn_state = await self.database.get_preset_state("porn")
                if porn_state and porn_state.get("enabled"):
                    p_scope = porn_state.get("scope", _db.DNS_SCOPE_GLOBAL)
                    p_sid = porn_state.get("scope_id")
                    dns_servers.append((p_scope, p_sid, "1.1.1.3"))
                    dns_servers.append((p_scope, p_sid, "1.0.0.3"))
                for user in users.values():
                    if user.dns_server:
                        dns_servers.append((_db.DNS_SCOPE_USER, user.id, user.dns_server))
                for dev in devices:
                    if dev.dns_server:
                        dns_servers.append((_db.DNS_SCOPE_DEVICE, dev.id, dev.dns_server))
                static_leases_list = await self.database.list_static_leases()
                static_map = {r["mac"]: (r.get("ip", ""), r.get("ip6", ""))
                              for r in static_leases_list}
                await asyncio.to_thread(
                    manager.apply, devices, rules, dns_servers, device_ids_by_user,
                    static_map)
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

    async def _extract_node_server_ip(self, node: Any) -> str | None:
        srv = None
        if getattr(node, "config_json", None):
            try:
                c = json.loads(node.config_json)
                srv = c.get("server")
                if not srv and "outbounds" in c and isinstance(c["outbounds"], list):
                    for ob in c["outbounds"]:
                        if isinstance(ob, dict) and ob.get("server"):
                            srv = ob["server"]
                            break
            except Exception:
                pass
        if not srv and getattr(node, "raw_uri", None):
            from urllib.parse import urlparse
            try:
                srv = urlparse(node.raw_uri).hostname
            except Exception:
                pass
        if not srv:
            return None
        srv_str = str(srv).strip()
        try:
            ip_address(srv_str)
            return srv_str
        except ValueError:
            # Resolved on EVERY tick for every saved node: an unresolvable or
            # slow hostname used to cost its full resolver timeout each time
            # (the live box: _sync_vpn_share took 40 s per tick, delaying
            # blocks/shaping/routing). Cache hits AND misses for a while and
            # cap a lookup at 3 s.
            cache = self.__dict__.setdefault("_node_ip_cache", {})
            now = time.monotonic()
            hit = cache.get(srv_str)
            if hit is not None and now - hit[0] < (600 if hit[1] else 120):
                return hit[1]
            try:
                resolved = await asyncio.wait_for(
                    asyncio.to_thread(socket.gethostbyname, srv_str), 3.0)
            except Exception:  # noqa: BLE001 — timeout or NXDOMAIN
                resolved = None
            cache[srv_str] = (now, resolved or None)
            return resolved or None

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
                        try:
                            nodes = await self.database.list_vpn_nodes()
                            if nodes:
                                nips = await asyncio.gather(
                                    *(self._extract_node_server_ip(n) for n in nodes),
                                    return_exceptions=True
                                )
                                for nip in nips:
                                    if isinstance(nip, str) and nip:
                                        self._vpn_allowed.add(nip)
                        except Exception as ex:
                            log.warning("Could not add saved VPN nodes to gw_allowed: %s", ex)
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
        # The tunnel flipped: reshape NOW (VPN margins / outer-download
        # policer on or off) instead of running 15 s at the wrong rates.
        await self._reshaping_now()

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
            "public_ip6": "", "ipv6_active": False,
        }
        if getattr(self, "ipv6_probe", None) is not None:
            st6 = self.ipv6_probe.get_status()
            out["public_ip6"] = st6.get("wan_ipv6_address") or ""
            out["ipv6_active"] = bool(st6.get("wan_ipv6_active"))
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
        if hasattr(self, "vpn_core") and self.vpn_core is not None:
            try:
                await asyncio.wait_for(self.vpn_core.disconnect(user_initiated=False), timeout=3.0)
            except Exception as e:
                log.debug("VPN core shutdown notice: %s", e)
        if self.ipv6_probe is not None and hasattr(self.ipv6_probe, "stop"):
            try:
                await self.ipv6_probe.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("IPv6 probe shutdown notice: %s", e)
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
        # Fast core boot: connects DB, re-arms config/bundle, instantiates managers (<20 ms)
        # Port 8080 binds immediately so web UI never gets ERR_CONNECTION_REFUSED on restart.
        await gateway.startup_core()
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
                         tailscale_manager=TailscaleManager(
                             on_system_change=gateway._network_heal),
                         firewall=gateway.firewall,
                         firewall_wan_preapply=gateway._firewall_wan_preapply,
                         updater=gateway.updater,
                         vpn_manager=gateway.vpn_manager,
                         engine=gateway.engine,
                         web_config=cfg.web,
                         waf_config=cfg.waf,
                         ipv6_probe=lambda: gateway.ipv6_probe,
                         ipv6_map_getter=gateway._device_ipv6_map,
                         ipv6_direct_getter=lambda: sorted(gateway._ipv6_direct_macs))
        # TLS (web.tls_certfile / web.tls_keyfile): uvicorn terminates HTTPS
        # itself — a self-signed cert is fine (one-time trust step in the
        # browser). When TLS is on, secure_cookies is forced True by the app.
        server_config = uvicorn.Config(
            app,
            host=cfg.web.host,
            port=cfg.web.port,
            log_level="warning",
            timeout_graceful_shutdown=2,
            ssl_certfile=cfg.web.tls_certfile or None,
            ssl_keyfile=cfg.web.tls_keyfile or None,
        )
        gateway.vpn_core = getattr(app.state, "vpn_manager", None)
        server = uvicorn.Server(server_config)

        async def _stop_listener() -> None:
            await gateway._stop_event.wait()
            server.should_exit = True

        stop_task = asyncio.create_task(_stop_listener())
        # Launch kernel/subsystem background init concurrently with web server
        subsystems_task = asyncio.create_task(gateway.startup_subsystems())
        try:
            await server.serve()
        finally:
            if not subsystems_task.done():
                subsystems_task.cancel()
                try:
                    await subsystems_task
                except asyncio.CancelledError:
                    pass
            stop_task.cancel()
            await gateway.shutdown()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
