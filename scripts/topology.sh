#!/usr/bin/env bash
# ===========================================================================
#  Quota Manager — runtime topology applier (called by the dashboard WAN tab)
# ---------------------------------------------------------------------------
#  Applies the WAN ("strong": the box dials PPPoE) or LAN (box behind the
#  router) topology WITHOUT re-running the full setup script. Invoked by the
#  running app (which is root) so a non-technical user switches topologies
#  entirely from the panel. The app (quota/netmgr.py) passes every network
#  value explicitly via the environment — this script never guesses.
#
#  TOPO=wan    NIC -> client subnet (primary) + the old uplink IP KEPT as a
#              secondary alias so clients can still reach the router's admin
#              page (e.g. 192.168.1.1) through the box — automatic, no extra
#              commands on any device (pppd owns the WAN)
#              * writes /etc/ppp/peers/quota-wan + chap/pap-secrets (chmod 600)
#              * enables + starts quota-wan-ppp.service (dials ppp0)
#              * dnsmasq -> no router upstream (the box is the WAN terminator)
#  TOPO=lan    the REVERT — fixes the old bug where the WAN dial was never
#              turned off (a LAN re-run of setup left pppd stealing the default
#              route with persist + replacedefaultroute)
#              * stops + DISABLES quota-wan-ppp.service, kills stray pppd
#              * removes ppp0 addresses/routes
#              * NIC -> uplink IP + client alias + default route via the router
#              * dnsmasq -> router + 8.8.8.8 upstreams
#
#  Never restarts the app or touches config.yaml — the app owns both (it
#  patches config.yaml + the DB setting TOGETHER and restarts itself after).
#  Idempotent: safe to re-run.
# ===========================================================================

set -euo pipefail

log()  { echo -e "\e[1;36m[topology]\e[0m $*"; }
warn() { echo -e "\e[1;33m[topology]\e[0m $*"; }
die()  { echo -e "\e[1;31m[topology] $*\e[0m" >&2; exit 1; }

# --- every value comes from the app (quota/netmgr.py) ------------------------
TOPO="${TOPO:-}"
LAN_IF="${LAN_IF:-}"              # NIC carrying the client subnet
LAN_IP="${LAN_IP:-}"              # uplink IP; kept as a router-admin alias in WAN mode
LAN_CIDR="${LAN_CIDR:-24}"        # uplink prefix
SUBNET_MASK="${SUBNET_MASK:-255.255.255.0}"
CLIENT_IP="${CLIENT_IP:-}"        # client-subnet alias (clients' gateway)
CLIENT_NET="${CLIENT_NET:-}"      # client subnet CIDR
WAN_GATEWAY="${WAN_GATEWAY:-}"    # upstream router IP
UPSTREAM_DNS="${UPSTREAM_DNS:-8.8.8.8}"
POOL_START="${POOL_START:-}"
POOL_END="${POOL_END:-}"
LEASE_HOURS="${LEASE_HOURS:-24}"
WAN_IF="${WAN_IF:-}"              # two-NIC layout: the NIC reaching the ONT/modem
PPPOE_USER="${PPPOE_USER:-}"      # PPPoE credentials from the WAN tab (env ONLY,
PPPOE_PASSWORD="${PPPOE_PASSWORD:-}"  # never argv — they must not show in `ps`)

[ "$(id -u)" -eq 0 ] || die "must run as root"
case "$TOPO" in wan|lan) ;; *) die "TOPO='$TOPO' must be 'wan' or 'lan'" ;; esac
[ -n "$LAN_IF" ] || die "LAN_IF not set (the app passes it)"
[ -n "$CLIENT_IP" ] || die "CLIENT_IP not set"
PPP_IF="${WAN_IF:-$LAN_IF}"

# ---------------------------------------------------------------------------
# NIC helpers — NetworkManager when it owns the interface, else ifupdown, and
# always `ip` to make the change live RIGHT NOW (NM may be mid-apply and an
# ifupdown file only takes effect on ifup/reboot).
# ---------------------------------------------------------------------------
_nm_profile() {
    # NetworkManager keys connections by PROFILE NAME, not interface.
    local p
    p="$(nmcli -t -f GENERAL.DEVICE,NAME con show 2>/dev/null \
         | awk -F: -v want="$LAN_IF" '$1 == want {print $2; exit}')" || true
    if [ -z "$p" ]; then
        p="$(nmcli -t -f NAME,DEVICE con show 2>/dev/null \
             | awk -F: -v want="$LAN_IF" '$2 == want {print $1; exit}')" || true
    fi
    echo "$p"
}

# Set a persistent static address set on the NIC + bring it live.
#   _nic_apply <primary-ip> <gateway> <dns> [extra-ip ...]
_nic_apply() {
    local primary="$1" gw="$2" dns="$3"; shift 3
    local extras=("$@") ip
    if command -v nmcli >/dev/null 2>&1 && nmcli general status >/dev/null 2>&1; then
        local profile
        profile="$(_nm_profile)"
        if [ -n "$profile" ]; then
            # Replace the address list with the primary, then re-add the extras.
            nmcli con mod "$profile" ipv4.method manual \
                ipv4.addresses "$primary" \
                ipv4.gateway "$gw" ipv4.dns "$dns" >/dev/null 2>&1 \
                || warn "nmcli could not set $primary on $LAN_IF"
            for ip in "${extras[@]:-}"; do
                [ -n "$ip" ] || continue
                nmcli con mod "$profile" -ipv4.addresses "$ip" >/dev/null 2>&1 || true
                nmcli con mod "$profile" +ipv4.addresses "$ip" >/dev/null 2>&1 || true
            done
            nmcli con up "$profile" >/dev/null 2>&1 || true
        else
            if ! nmcli con show "quota-gateway" >/dev/null 2>&1; then
                nmcli con add type ethernet con-name "quota-gateway" ifname "$LAN_IF" \
                    ipv4.method manual ipv4.addresses "$primary" >/dev/null 2>&1 || true
            fi
            nmcli con up "quota-gateway" >/dev/null 2>&1 || true
        fi
    else
        # No usable NetworkManager: persist via ifupdown (applies on reboot).
        # Secondary addresses are added on ifup / removed on ifdown.
        {
            printf 'auto %s\niface %s inet static\n' "$LAN_IF" "$LAN_IF"
            printf '    address %s\n    netmask %s\n' "$primary" "$SUBNET_MASK"
            [ -n "$gw" ] && printf '    gateway %s\n' "$gw"
            for ip in "${extras[@]:-}"; do
                [ -n "$ip" ] || continue
                printf '    up ip addr add %s dev %s\n' "$ip" "$LAN_IF"
                printf '    down ip addr del %s dev %s\n' "$ip" "$LAN_IF"
            done
        } > /etc/network/interfaces.d/quota-gateway
        warn "no NetworkManager — wrote /etc/network/interfaces.d/quota-gateway (applies on reboot)"
    fi
}

# Remove an IPv4 address if present (best-effort).
_ip_del() { ip addr show "$LAN_IF" 2>/dev/null | grep -q "inet $1/" \
            && ip addr del "$1/$LAN_CIDR" dev "$LAN_IF" >/dev/null 2>&1 || true; }
# Add an IPv4 address if absent (best-effort).
_ip_add() { ip addr show "$LAN_IF" 2>/dev/null | grep -q "inet $1/" \
            || ip addr add "$1/$LAN_CIDR" dev "$LAN_IF" >/dev/null 2>&1 || true; }

_nic_wan() {
    log "NIC $LAN_IF -> client subnet $CLIENT_IP/$LAN_CIDR + router-admin alias $LAN_IP/$LAN_CIDR"
    _nic_apply "$CLIENT_IP/$LAN_CIDR" "" "" "$LAN_IP/$LAN_CIDR"
    ip route del default 2>/dev/null || true   # pppd installs its own via ppp0
    _ip_add "$CLIENT_IP"
    # Keep the uplink IP as a SECONDARY address: the router is bridged in WAN
    # mode, so clients reach its admin page (${WAN_GATEWAY:-192.168.1.1}) through
    # the box's connected route to the uplink subnet — automatic router access,
    # no extra commands on any device. NOT a bypass: the masquerade only covers
    # $CLIENT_NET, so an uplink-subnet static source is never NATed out ppp0 and
    # cannot reach the internet through the box.
    [ -n "$LAN_IP" ] && _ip_add "$LAN_IP"
    ip addr show "$LAN_IF" 2>/dev/null | grep -q "inet $CLIENT_IP/" \
        || die "interface $LAN_IF does not carry $CLIENT_IP — WAN mode is dead without it"
}

_nic_lan() {
    log "NIC $LAN_IF -> uplink $LAN_IP/$LAN_CIDR + client alias $CLIENT_IP/$LAN_CIDR"
    _nic_apply "$LAN_IP/$LAN_CIDR" "$WAN_GATEWAY" "$UPSTREAM_DNS" "$CLIENT_IP/$LAN_CIDR"
    _ip_add "$LAN_IP"
    _ip_add "$CLIENT_IP"
    [ -n "$WAN_GATEWAY" ] || die "WAN_GATEWAY not set (the app passes it)"
    ip route replace default via "$WAN_GATEWAY" dev "$LAN_IF"
    for expect in "$LAN_IP" "$CLIENT_IP"; do
        ip addr show "$LAN_IF" 2>/dev/null | grep -q "inet $expect/" \
            || die "interface $LAN_IF does not carry $expect — LAN mode is dead without it"
    done
}

# ---------------------------------------------------------------------------
# dnsmasq — WAN forwards straight to UPSTREAM_DNS; LAN also uses the router.
# ---------------------------------------------------------------------------
_dnsmasq_wan() {
    log "dnsmasq -> WAN mode (upstream $UPSTREAM_DNS)"
    cat > /etc/dnsmasq.d/quota-gateway.conf <<EOF
# Quota Manager gateway — WAN mode (no router on the LAN; the box dials PPPoE)
interface=$LAN_IF
bind-interfaces
# We are the only DHCP server on this L2 (the AP's is disabled). Be
# authoritative so a client that reconnects still holding a stale lease is
# NAKed and re-DISCOVERs onto $CLIENT_NET immediately.
dhcp-authoritative
# DHCP: hand devices IPs on the CLIENT subnet with gateway + DNS = THIS laptop
# dhcp-sequential-ip: allocate STRICTLY in order from POOL_START (the dnsmasq
# default hashes by MAC across the whole pool -> gapped leases like .155/.185)
dhcp-sequential-ip
dhcp-range=$POOL_START,$POOL_END,$SUBNET_MASK,${LEASE_HOURS}h
dhcp-option=3,$CLIENT_IP          # default gateway = the quota laptop
dhcp-option=6,$CLIENT_IP          # DNS = the quota laptop (its dnsmasq forwards)
# DNS: relay upstream. The box terminates the WAN, so there is no router
# resolver on the LAN — forward straight to $UPSTREAM_DNS.
no-resolv
server=$UPSTREAM_DNS
# Log new leases so Quota Manager can learn MAC<->IP bindings
log-dhcp
dhcp-leasefile=/var/lib/misc/dnsmasq.leases
EOF
}

_dnsmasq_lan() {
    log "dnsmasq -> LAN mode (upstreams $WAN_GATEWAY + $UPSTREAM_DNS)"
    cat > /etc/dnsmasq.d/quota-gateway.conf <<EOF
# Quota Manager gateway
interface=$LAN_IF
bind-interfaces
# We are the only DHCP server on this L2 (the router's is disabled). Be
# authoritative so a client that reconnects still holding the router's old
# 192.168.1.x lease is NAKed and re-DISCOVERs onto $CLIENT_NET immediately
# instead of keeping its bypassing gateway until the old lease expires.
dhcp-authoritative
# DHCP: hand devices IPs on the CLIENT subnet with gateway + DNS = THIS laptop
# dhcp-sequential-ip: allocate STRICTLY in order from POOL_START (the dnsmasq
# default hashes by MAC across the whole pool -> gapped leases like .155/.185)
dhcp-sequential-ip
dhcp-range=$POOL_START,$POOL_END,$SUBNET_MASK,${LEASE_HOURS}h
dhcp-option=3,$CLIENT_IP          # default gateway = the quota laptop
dhcp-option=6,$CLIENT_IP          # DNS = the quota laptop (its dnsmasq forwards)
# DNS: relay upstream (Android uses the gateway as a resolver; answer it).
no-resolv
server=$WAN_GATEWAY
server=$UPSTREAM_DNS
# Log new leases so Quota Manager can learn MAC<->IP bindings
log-dhcp
dhcp-leasefile=/var/lib/misc/dnsmasq.leases
EOF
}

_dnsmasq_reload() {
    if dnsmasq --test -C /etc/dnsmasq.d/quota-gateway.conf >/dev/null 2>&1; then
        systemctl restart dnsmasq || warn "dnsmasq restart failed — run manually"
    else
        warn "dnsmasq config did not validate — fix it before starting the app"
    fi
}

# ---------------------------------------------------------------------------
# PPPoE (WAN) — peer + secrets (chmod 600) + the dial service.
# ---------------------------------------------------------------------------
_pppoe_wan() {
    log "writing PPPoE config for $PPP_IF (service quota-wan-ppp)"
    mkdir -p /etc/ppp/peers
    # `noauth` is REQUIRED: pppd enables `auth` (require the PEER to authenticate)
    # by default whenever a `user` + secrets file are present, and the BRAS
    # refuses to authenticate to the client ("peer refused to authenticate:
    # terminating link"). noauth keeps the client-side PAP/CHAP auth working.
    if [ -n "$PPPOE_USER" ]; then
        cat > /etc/ppp/peers/quota-wan <<EOF
# Quota Manager PPPoE peer (WAN mode) — dialed by quota-wan-ppp.service
plugin pppoe.so
$PPP_IF
persist
maxfail 0
defaultroute
replacedefaultroute
usepeerdns
mtu 1492
mru 1492
noipdefault
hide-password
noauth
user "$PPPOE_USER"
EOF
        cat > /etc/ppp/chap-secrets <<EOF
"$PPPOE_USER" * "$PPPOE_PASSWORD" *
EOF
        cat > /etc/ppp/pap-secrets <<EOF
"$PPPOE_USER" * "$PPPOE_PASSWORD" *
EOF
        chmod 600 /etc/ppp/chap-secrets /etc/ppp/pap-secrets
    else
        cat > /etc/ppp/peers/quota-wan <<EOF
# Quota Manager PPPoE peer (WAN mode) — dialed by quota-wan-ppp.service
plugin pppoe.so
$PPP_IF
persist
maxfail 0
defaultroute
replacedefaultroute
usepeerdns
mtu 1492
mru 1492
noipdefault
hide-password
noauth
EOF
        warn "PPPOE_USER not set — dialing with 'noauth', which most ISP lines reject"
    fi
    if ! command -v pppd >/dev/null 2>&1 && [ ! -x /usr/sbin/pppd ]; then
        die "pppd not found — install the 'ppp' package first (WAN mode needs it)"
    fi
    cat > /etc/systemd/system/quota-wan-ppp.service <<EOF
[Unit]
Description=Quota Manager PPPoE WAN (dial $PPP_IF -> ppp0)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=5
# nodetach keeps pppd in the foreground: pppd daemonizes by default after the
# link is up, so Type=simple + Restart=always would kill the daemon 5 s later
# and re-dial forever (an infinite connect/disconnect loop on the line).
ExecStart=/usr/sbin/pppd call quota-wan nodetach
StandardOutput=null
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable quota-wan-ppp >/dev/null 2>&1 \
        || warn "could not enable quota-wan-ppp.service"
    systemctl restart quota-wan-ppp >/dev/null 2>&1 \
        || warn "could not start quota-wan-ppp.service — check 'journalctl -u quota-wan-ppp -f'"
    log "   quota-wan-ppp.service enabled + started"
}

_pppoe_lan() {
    log "stopping + DISABLING the PPPoE dial (the old revert never did this)"
    if [ -e /etc/systemd/system/quota-wan-ppp.service ]; then
        systemctl disable quota-wan-ppp >/dev/null 2>&1 || true
        systemctl stop quota-wan-ppp >/dev/null 2>&1 || true
    fi
    pkill -f "pppd call quota-wan" >/dev/null 2>&1 || true
    # No ppp0 after the revert — drop any leftover address/route it left behind.
    ip addr flush dev ppp0 2>/dev/null || true
    ip link set ppp0 down 2>/dev/null || true
    ip route flush dev ppp0 2>/dev/null || true
    log "   quota-wan-ppp disabled, ppp0 cleaned"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
if [ "$TOPO" = "wan" ]; then
    _nic_wan
    _pppoe_wan
    _dnsmasq_wan
else
    _pppoe_lan
    _nic_lan
    _dnsmasq_lan
fi
_dnsmasq_reload

# The app restarts ITSELF after this script returns (quota/netmgr.py) so the
# engine rebuilds with the new topology; we must not.
log "topology '$TOPO' applied — the gateway service will now restart"
