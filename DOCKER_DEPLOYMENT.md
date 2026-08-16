# Docker Deployment Guide

Deploy Quota Manager as a Docker container on any Linux machine or home server (Debian, Ubuntu, Kali, Arch, Alpine, etc.).

---

## Architecture & How Containerized Gateway Works

Quota Manager is a **hardware-level network gateway, bandwidth controller, and DNS manager**. Unlike standard web services, it interacts directly with the Linux kernel networking subsystem:

| Subsystem | Purpose |
|---|---|
| `nftables` | Per-device packet accounting (`inet quota_gateway`) and client-subnet NAT masquerade (`inet quota_nat`) |
| `tc` (HTB + fq_codel) | Per-device and per-user speed limits and bufferbloat mitigation |
| `dnsmasq` | Authoritative DHCP server (UDP 67) and DNS filter/resolver (UDP 53) |
| ARP responder | Gateway lock and rogue device discovery |

### Docker Requirements

| Setting | Why It Is Required |
|---|---|
| `network_mode: host` | The container must attach directly to host network interfaces for raw ARP scanning, DHCP broadcast handling (UDP 67), DNS listening (UDP 53), and packet routing. **Note:** Standard Docker port mappings (`ports:`) are ignored in host networking mode. |
| `privileged: true` | Grants `NET_ADMIN` and `NET_RAW` capabilities to modify kernel firewall tables, configure traffic shaping queues (`tc`), and manage network routing. |

> [!WARNING]
> **Security & Shared Hosts Notice:** `privileged: true` with `network_mode: host` provides root-level network and kernel capability on the host system. This is intended for dedicated gateway appliances, standalone home servers, or single-purpose virtual machines. Platforms like TrueNAS SCALE, strict Kubernetes clusters, or multi-tenant hosting environments may restrict privileged containers.

---

## Network Prerequisites

Before launching the container, ensure your physical network is arranged:

1. **Wired Connection Required:** The host machine running Docker must connect to your home router with an **Ethernet cable** (a USB-to-Ethernet adapter works). WiFi cannot be used for the gateway interface.
2. **Turn Off Router DHCP:** Log in to your upstream router (e.g. `http://192.168.1.1`) and turn **DHCP OFF** (or configure a distinct fallback pool on `192.168.1.200+`). Devices keep connecting to the router's WiFi, but receive IP addresses, DNS, and gateway routing from Quota Manager.
3. **Disable IPv6 on Router:** Disable IPv6 / Router Advertisements (RA) on your router LAN. Quota Manager is IPv4-managed.

---

## Automatic Gateway Setup vs Host Setup

When started with `privileged: true` and `network_mode: host`, the container entrypoint **automatically initializes** the required gateway networking on first boot:

- Enables IPv4 kernel forwarding (`net.ipv4.ip_forward=1`).
- Disables IPv6 on the gateway interface.
- Assigns the client subnet gateway IP alias (e.g. `192.168.2.1/24`) to your Ethernet interface (`LAN_IF`).
- Configures `nftables` NAT masquerade table (`inet quota_nat`) so client traffic is NAT'd out to your upstream router.
- Generates `/etc/dnsmasq.d/quota-gateway.conf` with DHCP range, gateway, DNS options, and upstream servers.
- Configures DNS query logging (`/var/log/quota-dnsmasq.log`) for real-time per-device browsing history.

### Configuration Environment Variables

You can customize the network topology via environment variables in `docker-compose.yml` or `.env`:

| Variable | Default | Description |
|---|---|---|
| `QUOTA_PORT` | `8080` | Web dashboard HTTP port |
| `TZ` | `Africa/Cairo` | Timezone for logs and quota reset calculations |
| `LAN_IF` | Auto-detected | Physical Ethernet interface facing the LAN (e.g. `eth0`, `enp3s0`) |
| `CLIENT_IP` | `192.168.2.1` | Gateway IP address assigned to this box for the client subnet |
| `LAN_CIDR` | `24` | Subnet prefix length for the client network |
| `CLIENT_NET` | `192.168.2.0/24` | Client subnet CIDR (used for NAT masquerade) |
| `POOL_START` | `192.168.2.100` | First IP address in the DHCP pool |
| `POOL_END` | `192.168.2.200` | Last IP address in the DHCP pool |
| `WAN_GATEWAY` | `192.168.1.1` | Upstream ISP router IP address |
| `UPSTREAM_DNS` | `8.8.8.8` | Upstream DNS resolver (alongside `WAN_GATEWAY`) |
| `QUOTA_AUTO_GATEWAY` | `1` | Set to `0` if you configure host networking & NAT manually |

---

## Manual Host Setup Example (Optional)

If you set `QUOTA_AUTO_GATEWAY=0` or prefer to manage host-level network configuration yourself:

```bash
# 1. Enable IPv4 forwarding
sudo sysctl -w net.ipv4.ip_forward=1

# 2. Add client subnet alias to your wired NIC (e.g. eth0)
sudo ip addr add 192.168.2.1/24 dev eth0

# 3. Create nftables NAT masquerade table
sudo nft add table inet quota_nat
sudo nft add chain inet quota_nat postrouting '{ type nat hook postrouting priority 100; policy accept; }'
sudo nft add rule inet quota_nat postrouting ip saddr 192.168.2.0/24 masquerade
```

---

## Deployment Option A — Pre-built Image (⭐ Recommended)

Pull the ready-to-run multi-arch image (`amd64` / `arm64`) from GitHub Container Registry:

```yaml
services:
  quota-manager:
    image: ghcr.io/userjoo9/quotamanager:latest
    container_name: quota-manager
    network_mode: host
    privileged: true
    restart: unless-stopped
    environment:
      - TZ=Africa/Cairo
      - QUOTA_CONFIG=/app/config.yaml
      - QUOTA_PORT=8080
      - PYTHONUNBUFFERED=1
      # Network overrides (optional):
      # - LAN_IF=eth0
      # - CLIENT_IP=192.168.2.1
      # - WAN_GATEWAY=192.168.1.1
    volumes:
      # Replace /opt/quota-manager with your preferred host path:
      - /opt/quota-manager/config.yaml:/app/config.yaml:rw
      - /opt/quota-manager/data:/var/lib/quota-gateway:rw
      - /opt/quota-manager/logs:/var/log/quota-gateway:rw
      - /opt/quota-manager/dnsmasq.d:/etc/dnsmasq.d:rw
      - /opt/quota-manager/leases:/var/lib/misc:rw
```

**Upgrade image:**
```bash
docker compose pull && docker compose up -d
```

---

## Deployment Option B — Build from Source

Build directly from GitHub repository:

```yaml
services:
  quota-manager:
    image: quota-manager:custom
    pull_policy: build
    build:
      context: https://github.com/UserJoo9/QuotaManager.git#main
      dockerfile: Dockerfile
    container_name: quota-manager
    network_mode: host
    privileged: true
    restart: unless-stopped
    environment:
      - TZ=Africa/Cairo
      - QUOTA_CONFIG=/app/config.yaml
      - QUOTA_PORT=8080
      - PYTHONUNBUFFERED=1
    volumes:
      - /opt/quota-manager/config.yaml:/app/config.yaml:rw
      - /opt/quota-manager/data:/var/lib/quota-gateway:rw
      - /opt/quota-manager/logs:/var/log/quota-gateway:rw
      - /opt/quota-manager/dnsmasq.d:/etc/dnsmasq.d:rw
      - /opt/quota-manager/leases:/var/lib/misc:rw
```

**Upgrade source build:**
```bash
docker compose build --no-cache && docker compose up -d
```

---

## Deploying with Dockge / Portainer

1. Open your **Dockge** or **Portainer** dashboard and create a new stack named `quota-manager`.
2. Paste the compose YAML from **Option A** or **Option B**.
3. Adjust the volume storage paths (e.g. `/opt/quota-manager`).
4. Click **Deploy**.
5. Once running, open `http://<SERVER-IP>:8080` in your browser to complete initial setup.

---

## Persistent Volumes

All persistent data survives container updates and restarts:

| Host Path | Container Path | Purpose |
|---|---|---|
| `config.yaml` | `/app/config.yaml` | Application configuration (bundle, quotas, thresholds) |
| `data/` | `/var/lib/quota-gateway` | SQLite database (users, devices, allowances, usage logs) |
| `logs/` | `/var/log/quota-gateway` | Application activity and error logs |
| `dnsmasq.d/` | `/etc/dnsmasq.d` | Generated DNS filter rules and device tag mappings |
| `leases/` | `/var/lib/misc` | DHCP lease state file (`dnsmasq.leases`) |
