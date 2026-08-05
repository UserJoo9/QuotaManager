"""Quota Manager entrypoint.

Wires every layer together in one process. Two gateway profiles:

* **Windows** — userspace stack: packet engine (WinDivert) in a thread, async
  DHCP server (UDP 67), DNS forwarder (UDP 53), scapy proxy-ARP responder.
* **Linux (Kali/Debian, the target)** — the kernel owns the network path:
  dnsmasq serves DHCP + DNS on a dedicated client subnet (192.168.2.0/24,
  gateway = this box) which the kernel masquerades out the uplink, and the
  nftables engine (``quota/nftables.py``) counts + drops at line rate. Only
  the quota logic, API, DB and web UI are shared.

Always:
* Non-blocking logging (QueueHandler -> writer thread -> rotating file).
* SQLite (aiosqlite), quota service, per-period allowance snapshot.
* A maintenance coroutine that flushes engine counters to SQLite every 15 s,
  re-evaluates block states, and pushes fresh enforcement maps to the engine.
* FastAPI/uvicorn (REST + WebSocket push + static glassmorphism UI).

Any sub-component that cannot start (pydivert missing, no admin, no Npcap,
no ``nft``) degrades gracefully: the rest keeps running and the dashboard
still reports usage that has been flushed so far. This file never crashes the
whole app on an optional subsystem failure.

Usage
-----
    python run.py
    python run.py --config config-linux.yaml --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import logging
import platform
import signal
import time
from pathlib import Path

import uvicorn

from api.app import create_app
from core import config as cfg_mod
from core.logging_setup import setup_logging
from quota import db as _db
from quota.engine import EngineSnapshot, PacketEngine, SnapshotHolder
from quota.nftables import NftablesEngine
from quota.service import QuotaService

#: True on the Linux gateway. Changes which subsystems start: on Linux the
#: kernel (dnsmasq + nftables + NAT) replaces the userspace DHCP / DNS /
#: proxy-ARP stack, so those modules are imported lazily only where needed.
IS_LINUX = platform.system() == "Linux"

log = logging.getLogger("quota.run")


def _make_engine(cfg: cfg_mod.Config, holder) -> PacketEngine | NftablesEngine:
    """Build the accounting/block engine for this host.

    ``engine.backend`` selects the implementation: "auto" picks by OS
    (nftables on Linux, WinDivert on Windows); "windivert" / "nftables" force
    a specific one (the latter is handy on Linux and keeps Windows tests
    deterministic).
    """
    backend = getattr(cfg.engine, "backend", "auto")
    if backend == "auto":
        backend = "nftables" if IS_LINUX else "windivert"
    if backend == "nftables":
        return NftablesEngine(cfg, holder)
    return PacketEngine(
        cfg, holder, is_blocked_cb=lambda ip: False)  # live maps via update_state



def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Quota Manager gateway")
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("--port", type=int, default=None, help="override web port")
    p.add_argument("--debug", action="store_true", help="enable debug logging")
    return p.parse_args()


def _build_pool(dhcp_cfg) -> list[str]:
    """Expand 'pool_start..pool_end' into an explicit list of IPs."""
    return cfg_mod.expand_ip_range(dhcp_cfg.pool_start, dhcp_cfg.pool_end)


def _fallback_reserved(dhcp_cfg) -> set[str]:
    """The router's electric-cut fallback range, as a set our DHCP must avoid."""
    if not dhcp_cfg.fallback_enabled:
        return set()
    if not dhcp_cfg.fallback_pool_start or not dhcp_cfg.fallback_pool_end:
        log.warning("electric-cut fallback enabled but fallback_pool_start/end "
                    "not set — fallback is inactive")
        return set()
    return set(cfg_mod.expand_ip_range(dhcp_cfg.fallback_pool_start,
                                       dhcp_cfg.fallback_pool_end))


def _validate_fallback(dhcp_cfg) -> None:
    """Reject a misconfigured fallback range before DHCP binds (startup fatal)."""
    if not dhcp_cfg.fallback_enabled:
        return
    if not dhcp_cfg.fallback_pool_start or not dhcp_cfg.fallback_pool_end:
        return  # warned in _fallback_reserved; fallback is inactive
    pool = set(_build_pool(dhcp_cfg))
    fallback = set(cfg_mod.expand_ip_range(dhcp_cfg.fallback_pool_start,
                                           dhcp_cfg.fallback_pool_end))
    overlap = pool & fallback
    if overlap:
        raise ValueError(
            "electric-cut fallback pool overlaps the DHCP pool: "
            f"{sorted(overlap)[:5]}{'…' if len(overlap) > 5 else ''}. "
            "Give the router a range OUTSIDE the DHCP pool (e.g. "
            "192.168.1.201-250 when our pool is 192.168.1.100-200).")
    log.warning(
        "electric-cut fallback ACTIVE: the ROUTER must serve %s..%s "
        "(gateway=%s, no overlap with our pool) whenever this PC is down. "
        "Keep lease_hours short so devices return to the PC when it recovers.",
        dhcp_cfg.fallback_pool_start, dhcp_cfg.fallback_pool_end,
        dhcp_cfg.router_ip)


def _pc_mac_factory() -> str:
    """Best-effort PC MAC lookup, cached after first call."""
    cache: dict[str, str] = {}

    def _lookup() -> str:
        if "mac" in cache:
            return cache["mac"]
        try:
            import uuid
            mac = ":".join(f"{b:02x}" for b in uuid.getnode().to_bytes(6, "big"))
        except Exception:  # noqa: BLE001
            mac = "02:00:00:00:00:00"
        cache["mac"] = mac
        return mac

    return _lookup


class Gateway:
    """Owns all long-lived objects so tests can construct the wiring once."""

    def __init__(self, cfg: cfg_mod.Config) -> None:
        self.cfg = cfg
        self.database = _db.Database(cfg.db_path)
        self.service = QuotaService(self.database, timezone=cfg.timezone)
        self.holder = SnapshotHolder()
        self.engine: PacketEngine | NftablesEngine | None = None
        self.dhcp: object | None = None  # Windows-only (quota.dhcp.DhcpServer)
        self.dns: object | None = None   # Windows-only (quota.dns.DnsForwarder)
        self.arp: object | None = None   # Windows-only (quota.arp.ProxyArp)
        self._maintenance_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        #: Last known device IPs (updated by the maintenance loop; read by the
        #: proxy-ARP responder, which runs on a sniff thread and cannot await).
        self._known_ips: list[str] = []

    # ------------------------------------------------------------- startup

    async def startup(self) -> None:
        """Connect DB, ensure period, start optional subsystems."""
        await self.database.connect()
        await self._seed_bundle_from_cfg()
        await self.service.ensure_period()
        log.info("database ready: %s", self.cfg.db_path)

        # -- packet engine (thread on Windows, kernel rules on Linux) -------
        if self.cfg.engine.enabled:
            self.engine = _make_engine(self.cfg, self.holder)
            self.engine.start()
            log.info("packet engine started (%s)", getattr(self.engine, "name", "?"))
        else:
            log.warning("packet engine disabled in config — no per-packet accounting")

        # -- DHCP + DNS + proxy-ARP -----------------------------------------
        # On Linux these are the kernel's job: dnsmasq serves DHCP + DNS
        # (udp/67 + udp/53, no ICS war) on the dedicated client subnet, and
        # the setup-owned NAT table masquerades clients out the uplink. We
        # only LEARN device bindings from dnsmasq's lease file (see
        # _sync_dnsmasq_leases). On Windows the userspace stack below fills
        # the same role.
        if IS_LINUX:
            log.info("Linux gateway: DHCP/DNS via dnsmasq on client subnet "
                     "+ kernel NAT — Python DHCP/DNS/ARP stack skipped")
        else:
            # -- DNS forwarder (async, needs admin) -------------------------
            # Android/iOS treat the default gateway as a DNS resolver; without
            # a service on udp/53 every client query is dropped and devices
            # report "connected, no internet". The forwarder relays to
            # upstream resolvers.
            #
            # It binds the SPECIFIC gateway IP (not 0.0.0.0): Windows ICS
            # hosts a DNS proxy on 0.0.0.0:53 that silently drops queries from
            # non-ICS clients, and a more specific socket on the same port
            # with SO_REUSEADDR still receives the packets destined to that IP
            # — so we coexist with ICS instead of fighting over the port.
            from quota import dns as dns_mod  # lazy: scapy-free, no system deps

            advertise_self_dns = False
            if self.cfg.dhcp.enable and self.cfg.dhcp.dns_forward:
                self.dns = dns_mod.DnsForwarder(
                    upstreams=self.cfg.dhcp.dns_servers,
                    bind_host=self.cfg.dhcp.gateway_ip)
                asyncio.create_task(self._run_dns(self.dns))
                advertise_self_dns = True
            elif self.cfg.dhcp.enable and not self.cfg.dhcp.dns_forward:
                log.warning("DNS forwarder disabled in config — clients that "
                            "point at the gateway cannot resolve hostnames")

            # -- DHCP server (async, needs admin) --------------------------
            from quota import dhcp as dhcp_mod  # lazy

            if self.cfg.dhcp.enable:
                _validate_fallback(self.cfg.dhcp)  # fatal if fallback overlaps pool
                pool = _build_pool(self.cfg.dhcp)
                self.dhcp = dhcp_mod.DhcpServer(
                    cfg=self.cfg.dhcp,
                    pool=pool,
                    gateway=self.cfg.dhcp.gateway_ip,  # clients' gateway = the PC
                    subnet_mask=self.cfg.dhcp.subnet,
                    on_lease=self._on_lease,
                    reserved_ips=_fallback_reserved(self.cfg.dhcp),
                    advertise_self_dns=advertise_self_dns,
                )
                asyncio.create_task(self._run_dhcp(self.dhcp))
            else:
                log.warning("DHCP server disabled in config")

            # -- proxy-ARP (async, needs Npcap) ----------------------------
            from quota import arp as arp_mod  # lazy

            if self.cfg.arp.enabled:
                self.arp = arp_mod.ProxyArp(
                    interface=self.cfg.arp.interface,
                    get_device_ips=self._device_ips,
                    pc_mac=_pc_mac_factory(),
                    interval_sec=self.cfg.arp.announce_interval_sec,
                )
                asyncio.create_task(self.arp.start())
            else:
                log.warning("proxy-ARP disabled in config")

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
        await self.database.set_bundle(b)
        # Recompute the allowance snapshot too: an edit to config.yaml's
        # total_gb/reset_day must reach the per-device quotas, otherwise the
        # next evaluate_blocks run compares usage against yesterday's numbers.
        # recompute_allowances preserves period_start, so an existing period
        # and its recorded usage survive the resync.
        await self.service.recompute_allowances()
        log.info("bundle synced from config.yaml: %.1f GB, reset day %d",
                 b.total_gb, b.reset_day)

    # ------------------------------------------------------------- callbacks

    async def _run_dhcp(self, dhcp: object) -> None:
        try:
            await dhcp.start()
        except asyncio.CancelledError:
            raise
        except PermissionError as exc:
            log.error("DHCP server failed to start: %s", exc)
        except OSError as exc:
            log.error("DHCP server stopped: %s", exc)

    async def _run_dns(self, dns: object) -> None:
        try:
            await dns.start()
        except asyncio.CancelledError:
            raise
        except PermissionError as exc:
            log.error("DNS forwarder failed to start: %s", exc)
        except OSError as exc:
            log.error("DNS forwarder stopped: %s", exc)

    def _on_lease(self, mac: str, ip: str) -> None:
        """DHCP granted a lease -> persist the MAC<->IP binding."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._persist_lease(mac, ip))
        except RuntimeError:
            log.warning("lease for %s (%s) arrived with no running loop", mac, ip)

    async def _persist_lease(self, mac: str, ip: str) -> None:
        try:
            await self.database.upsert_lease(mac, ip, self.cfg.dhcp.lease_hours)
            self._known_ips.append(ip)
            # Auto-register unknown MACs so they appear in the dashboard.
            existing = await self.database.get_device(mac=mac)
            if existing is None:
                dev = await self.database.upsert_device(mac, name="")
                await self.database.add_event(
                    f"New device on network: {dev.mac} ({ip})", "info", dev.id)
                # A brand-new device must receive its allowance BEFORE the
                # next evaluate_blocks run, or allowances.get(mac) returns 0.0
                # and the device is instantly blocked for "quota exceeded".
                await self.service.recompute_allowances()
                log.info("auto-registered new device %s (%s)", mac, ip)
        except Exception:  # noqa: BLE001
            log.exception("failed to persist lease %s -> %s", mac, ip)

    async def _sync_dnsmasq_leases(self) -> None:
        """Linux: learn MAC<->IP bindings from dnsmasq's lease file.

        dnsmasq owns DHCP on the Linux gateway, so the async :meth:`_on_lease`
        callback never fires. Instead we parse its lease file every maintenance
        tick and feed each binding through the same :meth:`_persist_lease`
        path (DB upsert + auto-register unknown devices + allowances).
        """
        path = Path(self.cfg.dhcp.lease_file)
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
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

    def _device_ips(self) -> list[str]:
        """All IPs currently leased to devices (sync — called from a sniff thread)."""
        return list(self._known_ips)

    async def _refresh_known_ips(self) -> None:
        """Pull the latest lease IPs into the thread-safe cache."""
        try:
            leases = await self.database.list_leases()
            self._known_ips = [l.ip for l in leases]
        except Exception:  # noqa: BLE001
            log.exception("failed to refresh device IP cache")

    # ---------------------------------------------------------- maintenance

    async def _maintenance_loop(self) -> None:
        """Periodic flush + block re-evaluation + engine state sync."""
        interval = 15.0
        while not self._stop_event.is_set():
            try:
                await self._maintenance_tick()
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

        # 1b. Linux: learn device bindings from dnsmasq's lease file (this
        #     replaces the Windows DHCP on_lease callback).
        if IS_LINUX:
            await self._sync_dnsmasq_leases()

        # 2. Drain the packet engine's counters into usage_daily.
        #    flush() blocks: on Linux it shells out to `nft -j list counters`
        #    (a subprocess). Run it off the event loop so a slow nft never
        #    stalls the WebSocket push or the API. On Windows it is a quick
        #    lock-protected read — to_thread is harmless there.
        live_by_ip: dict[str, object] = {}
        if self.engine is not None:
            snap = await asyncio.to_thread(self.engine.flush)
            live_by_ip = snap.by_ip
            if snap.by_ip:
                today = _dt.date.today().isoformat()
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

        # 3. Recompute block states from usage vs allowances.
        changes = await self.service.evaluate_blocks()
        for ch in changes:
            log.warning("device %s (%s) -> %s", ch["device_id"], ch["mac"], ch["state"])

        # 3b. Refresh the thread-safe device IP cache used by proxy-ARP.
        await self._refresh_known_ips()

        # 4. Push fresh enforcement maps into the engine + holder.
        state = await self.service.snapshot_state()
        ip_to_mac = {v["ip"]: mac for mac, v in state.items() if v.get("ip")}
        blocked = {mac: v["blocked"] for mac, v in state.items()}
        if self.engine is not None:
            self.engine.update_state(ip_to_mac, blocked)
        # Live counters = the flushed delta, so the dashboard shows real
        # recent traffic instead of always-zero values.
        self.holder.swap(EngineSnapshot(
            by_ip=live_by_ip,
            ip_to_mac=ip_to_mac, blocked=blocked, ts=time.time()))

    # ------------------------------------------------------------- signals

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, self._request_stop)
            except NotImplementedError:
                pass  # Windows: add_signal_handler unsupported; uvicorn handles Ctrl+C

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
        if self.dns is not None:
            await self.dns.stop()
        if self.arp is not None:
            self.arp.stop()
        await self.database.close()
        log.info("shutdown complete")


def main() -> None:
    args = _parse_args()
    cfg = cfg_mod.load_config(args.config)
    if args.port is not None:
        cfg.web.port = args.port
    if args.debug:
        cfg.log_level = "DEBUG"

    setup_logging(cfg.log_level, cfg.log_file)
    log.info("Quota Manager starting (bundle %.1f GB, reset day %d)",
             cfg.bundle.total_gb, cfg.bundle.reset_day)

    gateway = Gateway(cfg)
    app = create_app(gateway.database, gateway.service, gateway.holder)

    server_config = uvicorn.Config(
        app,
        host=cfg.web.host,
        port=cfg.web.port,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)

    async def _serve() -> None:
        await gateway.startup()
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
