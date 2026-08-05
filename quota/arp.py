"""Proxy-ARP responder.

The one-armed gateway needs every packet touching a device to cross the PC.
Outbound traffic does this naturally (the PC is the devices' default gateway).
For return traffic, the router resolves a device's IP via ARP and may deliver
directly to the device — bypassing the PC. This module answers ARP
``who-has <device-ip>`` with the **PC's MAC**, so the router sends return
packets to the PC, which forwards them on-link to the device.

Requires Npcap (for scapy's raw L2 socket) and Administrator privileges.
If Npcap is unavailable the module degrades to a no-op and logs a warning —
quota counting then under-reports download but nothing breaks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

log = logging.getLogger("quota.arp")

# ARP operation codes
ARP_REQUEST = 1
ARP_REPLY = 2

ETH_P_ARP = 0x0806


class ProxyArp:
    """Answers ARP requests for device IPs on the LAN interface."""

    def __init__(
        self,
        interface: str,
        get_device_ips: Callable[[], list[str]],
        pc_mac: Callable[[], str],
        interval_sec: int = 60,
    ) -> None:
        self.interface = interface
        self.get_device_ips = get_device_ips
        self.get_pc_mac = pc_mac
        self.interval_sec = interval_sec
        self._stop = asyncio.Event()
        self._scapy_ready = False

    # -------------------------------------------------------------- helpers

    def _send_arp_reply(self, target_ip: str) -> bool:
        """Send a single ARP reply (target_ip -> PC MAC). Best effort."""
        if not self._scapy_ready:
            return False
        try:
            from scapy.all import ARP, Ether, sendp  # type: ignore

            pc_mac = self.get_pc_mac()
            pkt = Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(
                op=ARP_REPLY,
                hwsrc=pc_mac,
                psrc=target_ip,     # we claim to own this IP
                hwdst="ff:ff:ff:ff:ff:ff",
                pdst=target_ip,
            )
            sendp(pkt, iface=self.interface, verbose=False)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("arp send failed: %s", exc)
            return False

    def announce_all(self) -> None:
        """Gratuitous ARP replies for every known device IP."""
        for ip in self.get_device_ips():
            self._send_arp_reply(ip)

    # -------------------------------------------------------------- service

    async def start(self) -> None:
        try:
            from scapy.all import sniff, conf  # type: ignore
            if hasattr(conf, "iface") and self.interface:
                conf.iface = self.interface
            self._scapy_ready = True
            log.info("proxy-ARP enabled on %s", self.interface or "auto")
        except Exception as exc:  # noqa: BLE001
            self._scapy_ready = False
            log.error("scapy/Npcap unavailable — proxy-ARP disabled: %s", exc)
            return  # degrade to no-op

        # Periodic gratuitous announce keeps the router's ARP cache fresh.
        async def _announcer():
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
                self.announce_all()

        task = asyncio.create_task(_announcer())

        def _on_packet(pkt: Any) -> None:
            try:
                from scapy.layers.l2 import ARP as ScapyARP  # type: ignore
                arp = pkt.getlayer(ScapyARP)
                if arp is None or arp.op != ARP_REQUEST:
                    return
                target_ip = str(arp.pdst)
                if target_ip in self.get_device_ips():
                    self._send_arp_reply(target_ip)
            except Exception:  # noqa: BLE001
                pass

        try:
            # sniff() blocks until stop_filter is satisfied; run it in a thread
            # so the asyncio loop (web server, DHCP) is never blocked.
            await asyncio.to_thread(
                sniff, prn=_on_packet, filter="arp", store=False,
                iface=self.interface or None,
                stop_filter=lambda p: self._stop.is_set(),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("arp sniff failed: %s", exc)
        finally:
            task.cancel()

    def stop(self) -> None:
        self._stop.set()
