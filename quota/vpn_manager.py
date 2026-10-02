"""Built-in VPN Subsystem Manager (sing-box engine).

Controls:
- sing-box process lifecycle (start, stop, auto-restart, health-check)
- Asynchronous stdout/stderr log capture into an in-memory ring buffer
- Clash API client on 127.0.0.1:9090 (real-time speeds, session traffic, per-device connections)
- Node latency / ping probing
- Reconciles policy routing with VpnShareManager
"""

from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import json
import logging
import os
import subprocess
import socket
import tempfile
import time
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any
try:
    import httpx
except ImportError:
    httpx = None
import urllib.request

from quota.db import Database, VpnNode
from quota.vpn_parser import (
    find_sing_box_binary,
    generate_sing_box_config,
    validate_sing_box_config,
)

log = logging.getLogger("quota.vpn_manager")

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_ERROR = "error"


@dataclass
class ConnectedClientInfo:
    ip: str
    mac: str = ""
    device_name: str = ""
    user_name: str = ""
    active_connections: int = 0
    download_bytes: int = 0
    upload_bytes: int = 0
    routed: bool = True


@dataclass
class VpnManagerStatus:
    state: str = STATE_DISCONNECTED
    active_node_id: int | None = None
    active_node_name: str = ""
    protocol: str = ""
    uptime_seconds: int = 0
    speed_down_bps: int = 0
    speed_up_bps: int = 0
    total_down_bytes: int = 0
    total_up_bytes: int = 0
    active_connections: int = 0
    clients_count: int = 0
    clients: list[ConnectedClientInfo] = field(default_factory=list)
    last_error: str = ""
    interface: str = "quota-vpn"


def kill_stale_sing_box_processes() -> None:
    """Terminate any orphaned sing-box background processes left over from crashed / previous runs."""
    if os.name != "posix":
        return
    try:
        subprocess.run(["pkill", "-9", "-f", "sing-box run"], capture_output=True, timeout=2.0)
    except Exception:
        pass


class VpnManager:
    """Manages the internal sing-box core, Clash API polling, and log capture."""

    def __init__(self, db: Database, vpn_share_manager: Any = None) -> None:
        self.db = db
        self.vpn_share_manager = vpn_share_manager
        self.state = STATE_DISCONNECTED
        self.active_node: VpnNode | None = None
        self.start_time: float = 0.0
        self.last_error: str = ""
        self.tun_interface: str = "quota-vpn"
        self.clash_controller: str = "127.0.0.1:9090"
        self.mixed_port: int = 2081

        self._proc: asyncio.subprocess.Process | None = None
        self._config_file: str | None = None
        self._logs: collections.deque[dict[str, str]] = collections.deque(maxlen=400)
        self._append_log("[INFO] VPN subsystem initialized. Sing-box engine ready.")
        self._append_log(f"[INFO] Virtual interface: {self.tun_interface} | Local mixed port: {self.mixed_port}")
        self._monitor_task: asyncio.Task[None] | None = None
        self._log_reader_task: asyncio.Task[None] | None = None

        # Speed calculation
        self._last_traffic_poll: float = 0.0
        self._last_down_total: int = 0
        self._last_up_total: int = 0
        self._current_down_bps: int = 0
        self._current_up_bps: int = 0
        self._session_down_total: int = 0
        self._session_up_total: int = 0
        self._active_connections_count: int = 0
        self._cached_clients: list[ConnectedClientInfo] = []

    def get_logs(self, limit: int = 150) -> list[dict[str, str]]:
        """Return the most recent sing-box log lines."""
        all_logs = list(self._logs)
        return all_logs[-limit:] if limit > 0 else all_logs

    def _append_log(self, line: str) -> None:
        clean = line.strip()
        if not clean:
            return
        now_str = _dt.datetime.now().strftime("%H:%M:%S")
        self._logs.append({"time": now_str, "text": clean})
        if "FATAL" in clean or "panic" in clean.lower():
            self.last_error = clean

    async def ping_node(self, host: str, port: int, timeout: float = 3.0) -> float:
        """Measure TCP connect latency to host:port in milliseconds."""
        clean_host = (host or "").strip()
        if not clean_host or not port:
            return -1.0
        start = time.perf_counter()
        target_ip = clean_host
        try:
            # Force IPv4 to prevent IPv6 network unreachable errors on systems with unrouted IPv6
            try:
                ip_address(clean_host)
            except ValueError:
                target_ip = await asyncio.to_thread(socket.gethostbyname, clean_host)

            _, writer = await asyncio.wait_for(
                asyncio.open_connection(target_ip, int(port)), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return round((time.perf_counter() - start) * 1000.0, 1)
        except Exception as e:
            log.warning("Ping failed for %s (%s):%d [%s]: %s", clean_host, target_ip, port, type(e).__name__, e)
            return -1.0

    async def auto_restore(self) -> None:
        """Auto-restore persistent VPN tunnel on boot/reload if enabled."""
        try:
            auto_conn = await self.db.get_setting("vpn_auto_connect", "0")
            node_id_str = await self.db.get_setting("vpn_active_node_id", "")
            if auto_conn == "1" and node_id_str and node_id_str.isdigit():
                node_id = int(node_id_str)
                log.info("Auto-restoring persistent VPN connection to node #%d...", node_id)
                self._append_log(f"[INFO] Auto-restoring persistent VPN connection to node #{node_id}...")
                ok, msg = await self.connect(node_id)
                if ok:
                    log.info("Persistent VPN connection restored to node #%d", node_id)
                else:
                    log.warning("Failed to auto-restore VPN connection: %s", msg)
        except Exception as e:
            log.warning("VPN auto_restore failed: %s", e)

    async def connect(self, node_id: int) -> tuple[bool, str]:
        """Start sing-box with the configuration from node_id."""
        await self.disconnect(user_initiated=False)
        kill_stale_sing_box_processes()

        node = await self.db.get_vpn_node(node_id)
        if not node:
            return False, f"VPN Node #{node_id} not found"

        self._append_log("──────────────────────────────────────────────────")
        self._append_log(f"[INFO] Preparing connection to '{node.name}' ({node.protocol.upper()})...")

        bin_path = find_sing_box_binary()
        if not bin_path:
            msg = "sing-box binary not found. Please verify bin/sing-box or bin/sing-box.exe."
            self.state = STATE_ERROR
            self.last_error = msg
            self._append_log(f"[ERROR] {msg}")
            return False, msg

        # Parse / assemble config
        try:
            config_dict = json.loads(node.config_json)
            # If not a full config, build full sing-box config
            if "inbounds" not in config_dict or "outbounds" not in config_dict:
                allow_insecure = (await self.db.get_setting("vpn_allow_insecure", "0")) == "1"
                self._append_log(f"[INFO] Assembling full routing profile (allow_insecure={allow_insecure})...")
                config_dict = generate_sing_box_config(
                    config_dict,
                    tun_interface=self.tun_interface,
                    clash_controller=self.clash_controller,
                    mixed_port=getattr(self, "mixed_port", 2081),
                    allow_insecure=allow_insecure,
                )
        except Exception as e:
            msg = f"Failed to assemble sing-box config: {e}"
            self.state = STATE_ERROR
            self.last_error = msg
            self._append_log(f"[ERROR] {msg}")
            return False, msg

        # Validate config with sing-box check
        self._append_log("[INFO] Validating configuration with sing-box core check...")
        valid, val_err = validate_sing_box_config(config_dict, bin_path)
        if not valid:
            msg = f"sing-box configuration validation failed: {val_err}"
            self.state = STATE_ERROR
            self.last_error = msg
            self._append_log(f"[ERROR] {msg}")
            return False, msg

        self._append_log("[INFO] Configuration validated successfully. Spawning core process...")

        # Write config file to temporary directory
        tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config_dict, tf, indent=2)
        tf.close()
        self._config_file = tf.name

        self.state = STATE_CONNECTING
        self.active_node = node
        self.last_error = ""

        # Ensure execution permission
        if os.name == "posix" and not os.access(bin_path, os.X_OK):
            try:
                os.chmod(bin_path, 0o755)
            except Exception as e:
                log.warning("Could not auto-chmod %s: %s", bin_path, e)

        # Spawn sing-box process
        try:
            work_dir = str(Path(bin_path).parent)
            env = os.environ.copy()
            env["ENABLE_DEPRECATED_OUTBOUND_DNS_RULE_ITEM"] = "true"
            self._proc = await asyncio.create_subprocess_exec(
                bin_path,
                "run",
                "-c",
                self._config_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work_dir,
                env=env,
            )
            self._append_log(f"[INFO] sing-box core started (PID: {self._proc.pid}). Awaiting tunnel establishment...")
            self.start_time = time.time()
            self._last_traffic_poll = self.start_time
            self._last_down_total = 0
            self._last_up_total = 0
            self._session_down_total = 0
            self._session_up_total = 0

            # Start background stdout/stderr log consumer
            self._log_reader_task = asyncio.create_task(self._read_process_logs())

            # Start background Clash API monitor
            self._monitor_task = asyncio.create_task(self._poll_monitor_loop())

            # Brief pause to verify immediate launch success
            await asyncio.sleep(0.5)
            if self._proc.returncode is not None:
                err_line = self.last_error or f"Process exited immediately with code {self._proc.returncode}"
                self.state = STATE_ERROR
                self.last_error = err_line
                return False, err_line

            self.state = STATE_CONNECTED
            self._append_log(f"[INFO] VPN core connected successfully. Interface: {self.tun_interface}")

            # Persist VPN share enabled, active node and pinned interface in DB
            await self.db.set_setting("vpn_share_enabled", "1")
            await self.db.set_setting("vpn_share_interface", self.tun_interface)
            await self.db.set_setting("vpn_active_node_id", str(node.id))
            await self.db.set_setting("vpn_auto_connect", "1")

            # Reconcile policy routing
            try:
                await self._reconcile_policy_routing()
            except Exception as ex:
                log.warning("Policy routing reconciliation notice: %s", ex)

            return True, "Connected successfully"

        except Exception as e:
            self.state = STATE_ERROR
            self.last_error = str(e)
            self._append_log(f"[ERROR] Launch failed: {e}")
            await self.disconnect(user_initiated=False)
            return False, str(e)

    async def disconnect(self, user_initiated: bool = True) -> None:
        """Stop the sing-box process and tear down policy routing."""
        self.state = STATE_DISCONNECTED
        self.active_node = None
        self._current_down_bps = 0
        self._current_up_bps = 0
        self._active_connections_count = 0
        self._cached_clients = []

        if self._log_reader_task and not self._log_reader_task.done():
            self._log_reader_task.cancel()
            self._log_reader_task = None

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None

        had_proc = self._proc is not None
        if self._proc:
            self._append_log("[INFO] Stopping VPN process...")
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, TimeoutError):
                    self._proc.kill()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                    except (asyncio.TimeoutError, TimeoutError):
                        log.warning("VPN process kill timed out")
            except Exception as e:
                log.debug("Error while stopping sing-box process: %s", e)
            finally:
                self._proc = None

        # Clean any remaining sing-box instances only if a process was active
        if had_proc:
            kill_stale_sing_box_processes()

        # Clean up temp config file
        if self._config_file:
            try:
                Path(self._config_file).unlink(missing_ok=True)
            except Exception:
                pass
            self._config_file = None

        if user_initiated:
            # Reset VPN share settings in DB only on user-initiated disconnect
            await self.db.set_setting("vpn_share_enabled", "0")
            await self.db.set_setting("vpn_share_interface", "")
            await self.db.set_setting("vpn_active_node_id", "")
            await self.db.set_setting("vpn_auto_connect", "0")

        # Flush policy routing in VpnShareManager if attached
        if self.vpn_share_manager:
            try:
                if hasattr(self.vpn_share_manager, "reconcile"):
                    await asyncio.wait_for(asyncio.to_thread(self.vpn_share_manager.reconcile, False), timeout=3.0)
                elif hasattr(self.vpn_share_manager, "flush"):
                    await asyncio.wait_for(asyncio.to_thread(self.vpn_share_manager.flush), timeout=3.0)
            except Exception as ex:
                log.debug("Policy routing flush notice: %s", ex)

        self._append_log("[INFO] VPN disconnected.")

    async def _read_process_logs(self) -> None:
        """Continuously read process stdout/stderr lines."""
        if not self._proc or not self._proc.stdout:
            return
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._append_log(text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("Log reader exception: %s", e)

    async def _poll_monitor_loop(self) -> None:
        """Poll Clash API on 127.0.0.1:9090 to update live metrics and connected clients."""
        clash_url = f"http://{self.clash_controller}/connections"
        while self.state in (STATE_CONNECTING, STATE_CONNECTED):
            try:
                # Check if process is still alive
                if self._proc and self._proc.returncode is not None:
                    self.state = STATE_ERROR
                    if not self.last_error:
                        self.last_error = f"sing-box stopped unexpectedly (exit code {self._proc.returncode})"
                    self._append_log(f"[FATAL] {self.last_error}")

                    # Attempt auto-reconnect if auto_connect is active
                    try:
                        auto_conn = await self.db.get_setting("vpn_auto_connect", "0")
                        node_id_str = await self.db.get_setting("vpn_active_node_id", "")
                        if auto_conn == "1" and node_id_str and node_id_str.isdigit():
                            self._append_log("[WARN] Auto-reconnecting VPN tunnel in 3 seconds...")
                            await asyncio.sleep(3.0)
                            if (await self.db.get_setting("vpn_auto_connect", "0")) == "1":
                                asyncio.create_task(self.connect(int(node_id_str)))
                    except Exception as ex:
                        log.debug("VPN auto-reconnect error: %s", ex)
                    break

                data = None
                if httpx is not None:
                    try:
                        async with httpx.AsyncClient(timeout=2.0) as client:
                            resp = await client.get(clash_url)
                            if resp.status_code == 200:
                                data = resp.json()
                    except Exception as e:
                        log.debug("Clash API poll notice (httpx): %s", e)
                else:
                    def _fetch_sync() -> dict[str, Any] | None:
                        req = urllib.request.Request(clash_url, headers={"User-Agent": "QuotaManager/1.0"})
                        with urllib.request.urlopen(req, timeout=2.0) as resp:
                            if resp.status == 200:
                                return json.loads(resp.read().decode("utf-8"))
                        return None
                    try:
                        data = await asyncio.to_thread(_fetch_sync)
                    except Exception as e:
                        log.debug("Clash API poll notice (urllib): %s", e)

                if data:
                    await self._process_clash_data(data)
                    if self.state == STATE_CONNECTING:
                        self.state = STATE_CONNECTED
            except Exception as e:
                log.debug("Clash API poll loop error: %s", e)

            await asyncio.sleep(2.0)

    async def _process_clash_data(self, data: dict[str, Any]) -> None:
        """Parse Clash API connections payload into per-device statistics."""
        now = time.time()
        dt = max(0.1, now - self._last_traffic_poll)
        self._last_traffic_poll = now

        down_total = int(data.get("downloadTotal", 0))
        up_total = int(data.get("uploadTotal", 0))

        if self._last_down_total > 0:
            delta_down = max(0, down_total - self._last_down_total)
            delta_up = max(0, up_total - self._last_up_total)
            self._current_down_bps = int((delta_down * 8) / dt)
            self._current_up_bps = int((delta_up * 8) / dt)

        self._last_down_total = down_total
        self._last_up_total = up_total
        self._session_down_total = down_total
        self._session_up_total = up_total

        connections = data.get("connections", [])
        self._active_connections_count = len(connections)

        # Aggregate by sourceIP
        ip_stats: dict[str, dict[str, int]] = {}
        for conn in connections:
            meta = conn.get("metadata", {})
            src_ip = meta.get("sourceIP", "")
            if not src_ip or src_ip in ("127.0.0.1", "::1"):
                continue
            st = ip_stats.setdefault(src_ip, {"conns": 0, "down": 0, "up": 0})
            st["conns"] += 1
            st["down"] += int(conn.get("download", 0))
            st["up"] += int(conn.get("upload", 0))

        # Map source IPs to DB devices and users
        devices = await self.db.list_devices()
        users = {u.id: u for u in await self.db.list_users()}
        static_leases = {sl["ip"]: sl for sl in await self.db.list_static_leases()}
        routing_rules = {f"{r.target_type}:{r.target_id}": r.route_vpn for r in await self.db.get_vpn_routing_rules()}

        clients_list: list[ConnectedClientInfo] = []
        for src_ip, st in ip_stats.items():
            dev_name = ""
            mac = ""
            user_name = ""
            user_id = None
            dev_id = None

            # Check static leases first
            if src_ip in static_leases:
                sl = static_leases[src_ip]
                mac = sl["mac"]
                dev_name = sl.get("hostname", "")

            # Match device by MAC or IP
            for d in devices:
                if mac and d.mac.lower() == mac.lower():
                    dev_name = dev_name or d.name or d.mac
                    dev_id = d.id
                    user_id = d.user_id
                    break

            if user_id and user_id in users:
                user_name = users[user_id].name

            # Determine routing status
            # Default is routed unless device or user has route_vpn=False in routing_rules or vpn_bypass=True
            is_routed = True
            if dev_id is not None and f"device:{dev_id}" in routing_rules:
                is_routed = routing_rules[f"device:{dev_id}"]
            elif user_id is not None and f"user:{user_id}" in routing_rules:
                is_routed = routing_rules[f"user:{user_id}"]

            clients_list.append(
                ConnectedClientInfo(
                    ip=src_ip,
                    mac=mac,
                    device_name=dev_name or src_ip,
                    user_name=user_name,
                    active_connections=st["conns"],
                    download_bytes=st["down"],
                    upload_bytes=st["up"],
                    routed=is_routed,
                )
            )

        clients_list.sort(key=lambda c: c.download_bytes, reverse=True)
        self._cached_clients = clients_list

    async def _calculate_bypass_ips(self) -> list[str]:
        """Collect current IPs for all devices/users marked for VPN bypass."""
        devices = await self.db.list_devices()
        users = {u.id: u for u in await self.db.list_users()}
        try:
            static_leases = {
                sl["mac"].lower(): sl["ip"]
                for sl in await self.db.list_static_leases()
                if sl.get("ip") and sl.get("mac")
            }
        except Exception:
            static_leases = {}

        excluded: set[str] = set()
        for d in devices:
            user = users.get(d.user_id) if d.user_id is not None else None
            if d.vpn_bypass or (user is not None and user.vpn_bypass):
                if d.mac.lower() in static_leases:
                    excluded.add(static_leases[d.mac.lower()])
                if getattr(d, "ip", None):
                    excluded.add(d.ip)
        return sorted(excluded)

    async def _reconcile_policy_routing(self) -> None:
        """Synchronize policy routing bypass rules with DB routing rules and kernel."""
        # Find all devices / users that have route_vpn=False
        rules = await self.db.get_vpn_routing_rules()
        bypass_device_ids = {r.target_id for r in rules if r.target_type == "device" and not r.route_vpn}
        bypass_user_ids = {r.target_id for r in rules if r.target_type == "user" and not r.route_vpn}

        # Sync bypass flag on devices/users in DB
        devices = await self.db.list_devices()
        for d in devices:
            should_bypass = (d.id in bypass_device_ids) or (d.user_id in bypass_user_ids)
            if d.vpn_bypass != should_bypass:
                await self.db.update_device(d.id, vpn_bypass=should_bypass)

        users = await self.db.list_users()
        for u in users:
            should_bypass = u.id in bypass_user_ids
            if u.vpn_bypass != should_bypass:
                await self.db.update_user(u.id, vpn_bypass=should_bypass)

        # Lazy init VpnShareManager if not attached and on POSIX
        if self.vpn_share_manager is None and os.name == "posix":
            try:
                from quota.vpnshare import VpnShareManager
                self.vpn_share_manager = VpnShareManager(None)
            except Exception as ex:
                log.debug("VpnShareManager lazy init note: %s", ex)

        # Apply kernel policy routing via VpnShareManager
        if self.vpn_share_manager and hasattr(self.vpn_share_manager, "reconcile"):
            is_on = self.state == STATE_CONNECTED
            bypass_ips = await self._calculate_bypass_ips() if is_on else []
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.vpn_share_manager.reconcile,
                        is_on,
                        self.tun_interface if is_on else "",
                        bypass_ips,
                    ),
                    timeout=5.0,
                )
            except Exception as ex:
                log.warning("VpnShareManager reconcile error: %s", ex)

    async def get_status(self) -> VpnManagerStatus:
        """Return the current VPN status dataclass."""
        uptime = int(time.time() - self.start_time) if self.state == STATE_CONNECTED else 0
        return VpnManagerStatus(
            state=self.state,
            active_node_id=self.active_node.id if self.active_node else None,
            active_node_name=self.active_node.name if self.active_node else "",
            protocol=self.active_node.protocol if self.active_node else "",
            uptime_seconds=uptime,
            speed_down_bps=self._current_down_bps,
            speed_up_bps=self._current_up_bps,
            total_down_bytes=self._session_down_total,
            total_up_bytes=self._session_up_total,
            active_connections=self._active_connections_count,
            clients_count=len(self._cached_clients),
            clients=self._cached_clients,
            last_error=self.last_error,
            interface=self.tun_interface,
        )
