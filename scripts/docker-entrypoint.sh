#!/usr/bin/env bash
set -e

# ==============================================================================
# QuotaManager Docker Entrypoint
# Initializes storage, networking prerequisites (IP forwarding, client alias,
# NAT masquerade), dnsmasq DHCP/DNS, and launches the application.
# ==============================================================================

# Ensure standard runtime directories exist
mkdir -p /var/lib/quota-gateway \
         /var/log/quota-gateway \
         /etc/dnsmasq.d \
         /var/lib/misc \
         /app/data

# Ensure log and lease files exist so tailers/sync don't throw FileNotFoundError
touch /var/log/quota-dnsmasq.log 2>/dev/null || true
touch /var/lib/misc/dnsmasq.leases 2>/dev/null || true
touch /var/log/quota-gateway/quota.log 2>/dev/null || true

# If config.yaml does not exist at /app/config.yaml or is empty, provide default template
CONFIG_FILE="${QUOTA_CONFIG:-/app/config.yaml}"
if [ -d "$CONFIG_FILE" ]; then
    echo "[docker-entrypoint] Config path ($CONFIG_FILE) is a directory (e.g. host bind mount)."
    if [ ! -s "$CONFIG_FILE/config.yaml" ] && [ ! -s "$CONFIG_FILE/config.yml" ]; then
        echo "[docker-entrypoint] Copying default template to $CONFIG_FILE/config.yaml..."
        if [ -f "/app/config.default.yaml" ]; then
            cp /app/config.default.yaml "$CONFIG_FILE/config.yaml"
        fi
    fi
    export QUOTA_CONFIG="$CONFIG_FILE/config.yaml"
elif [ ! -s "$CONFIG_FILE" ]; then
    echo "[docker-entrypoint] Config file ($CONFIG_FILE) not found or empty. Copying default template..."
    if [ -f "/app/config.default.yaml" ]; then
        cp /app/config.default.yaml "$CONFIG_FILE"
    fi
fi

# ==============================================================================
# Gateway Network Initialization (Enabled by default when container has privileges)
# Set QUOTA_AUTO_GATEWAY=0 to disable automatic host network/NAT configuration.
# ==============================================================================
if [ "${QUOTA_AUTO_GATEWAY:-1}" = "1" ]; then
    # Auto-detect LAN interface if not explicitly provided
    if [ -z "$LAN_IF" ]; then
        for cand in $(ip route 2>/dev/null | awk '/default/ {print $5}') \
                    $(ls /sys/class/net 2>/dev/null | grep -v '^lo$'); do
            [ -d "/sys/class/net/$cand/wireless" ] && continue
            # Skip virtual interfaces (bridges, veths) which lack a physical device link
            [ ! -L "/sys/class/net/$cand/device" ] && continue
            LAN_IF="$cand"
            break
        done
        if [ -z "$LAN_IF" ]; then
            echo "[docker-entrypoint] ERROR: Could not auto-detect a physical wired LAN interface." >&2
            echo "[docker-entrypoint] ERROR: Please specify LAN_IF explicitly in your environment." >&2
            exit 1
        fi
        echo "[docker-entrypoint] Auto-detected physical LAN interface: $LAN_IF"
    else
        echo "[docker-entrypoint] Using explicitly configured LAN interface: $LAN_IF"
    fi

    CLIENT_IP="${CLIENT_IP:-192.168.2.1}"
    LAN_CIDR="${LAN_CIDR:-24}"
    CLIENT_NET="${CLIENT_NET:-192.168.2.0/24}"
    POOL_START="${POOL_START:-192.168.2.100}"
    POOL_END="${POOL_END:-192.168.2.200}"
    SUBNET_MASK="${SUBNET_MASK:-255.255.255.0}"
    LEASE_HOURS="${LEASE_HOURS:-24}"
    WAN_GATEWAY="${WAN_GATEWAY:-192.168.1.1}"
    UPSTREAM_DNS="${UPSTREAM_DNS:-8.8.8.8}"

    # Surface the uplink subnet to remind the user about config.yaml
    UPLINK_IP=$(ip -4 addr show dev "$LAN_IF" 2>/dev/null | awk '/inet / {print $2}' | grep -v "^${CLIENT_IP}/" | head -n1)
    if [ -n "$UPLINK_IP" ]; then
        echo "[docker-entrypoint] IMPORTANT: Host uplink IP is $UPLINK_IP."
        echo "[docker-entrypoint] IMPORTANT: Ensure 'engine.uplink_subnet' in config.yaml matches this network to avoid metering local traffic."
    fi

    # 1. Enable IPv4 forwarding in kernel
    if [ -w /proc/sys/net/ipv4/ip_forward ]; then
        echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true
    fi

    # 2. Disable IPv6 on LAN interface (Quota Manager is IPv4-only)
    if [ -d "/proc/sys/net/ipv6/conf" ]; then
        echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || true
        if [ -d "/proc/sys/net/ipv6/conf/$LAN_IF" ]; then
            echo 1 > "/proc/sys/net/ipv6/conf/$LAN_IF/disable_ipv6" 2>/dev/null || true
        fi
    fi

    # 3. Add secondary Client IP alias on LAN interface if missing
    if ip link show "$LAN_IF" >/dev/null 2>&1; then
        if ! ip addr show "$LAN_IF" 2>/dev/null | grep -q "inet $CLIENT_IP/"; then
            echo "[docker-entrypoint] Assigning client gateway alias $CLIENT_IP/$LAN_CIDR to $LAN_IF..."
            ip addr add "$CLIENT_IP/$LAN_CIDR" dev "$LAN_IF" 2>/dev/null || true
        fi
    fi

    # 4. Configure nftables NAT masquerade table (inet quota_nat)
    if command -v nft >/dev/null 2>&1; then
        if ! nft list table inet quota_nat >/dev/null 2>&1; then
            echo "[docker-entrypoint] Creating nftables NAT masquerade table (inet quota_nat)..."
            nft -f - <<EOF 2>/dev/null || true
table inet quota_nat {
    chain postrouting {
        type nat hook postrouting priority 100; policy accept;
        ip saddr $CLIENT_NET masquerade
    }
}
EOF
        fi
    fi

    # 5. Generate base dnsmasq DHCP/DNS configuration if none exists
    if [ ! -s /etc/dnsmasq.d/quota-gateway.conf ]; then
        echo "[docker-entrypoint] Generating /etc/dnsmasq.d/quota-gateway.conf for $LAN_IF ($CLIENT_NET)..."
        cat > /etc/dnsmasq.d/quota-gateway.conf <<EOF
# Quota Manager Gateway DHCP & DNS configuration
interface=$LAN_IF
bind-interfaces
dhcp-authoritative
dhcp-sequential-ip
dhcp-range=$POOL_START,$POOL_END,$SUBNET_MASK,${LEASE_HOURS}h
dhcp-option=3,$CLIENT_IP
dhcp-option=6,$CLIENT_IP
no-resolv
server=$WAN_GATEWAY
server=$UPSTREAM_DNS
log-dhcp
dhcp-leasefile=/var/lib/misc/dnsmasq.leases
EOF
    fi

    # 6. Generate query logging configuration for per-device browsing history
    if [ ! -s /etc/dnsmasq.d/quota-dnslog.conf ]; then
        cat > /etc/dnsmasq.d/quota-dnslog.conf <<EOF
log-queries=extra
log-async=20
log-facility=/var/log/quota-dnsmasq.log
EOF
    fi

    # 7. Prepare empty domain-filtering files if not present
    for f in quota-tags.conf quota-domains.conf; do
        if [ ! -f "/etc/dnsmasq.d/$f" ]; then
            printf '# Quota Manager — generated, do not edit by hand.\n' > "/etc/dnsmasq.d/$f"
        fi
    done
fi

# Start internal dnsmasq daemon if not already running on port 53
if command -v dnsmasq >/dev/null 2>&1; then
    if ! pidof dnsmasq >/dev/null 2>&1; then
        echo "[docker-entrypoint] Starting dnsmasq daemon..."
        dnsmasq --conf-dir=/etc/dnsmasq.d,*.conf 2>/dev/null || true
    fi
fi

# Execute main process passed to container
exec "$@"
