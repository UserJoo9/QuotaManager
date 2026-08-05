#!/usr/bin/env bash
# ===========================================================================
#  Quota Manager — gateway setup for a Debian/Kali laptop (wired, one NIC)
# ---------------------------------------------------------------------------
#  Run as root on the gateway laptop:
#      sudo bash scripts/setup_gateway_kali.sh
#
#  IMPORTANT ORDER: create the project venv and install the Python deps BEFORE
#  running this script — the systemd unit it writes uses $APP_DIR/.venv/bin/python3
#  ONLY if the venv already exists at that point; otherwise it falls back to the
#  system python3, which does not have the app's dependencies and the service
#  will fail to start. If you already ran this without the venv, create it and
#  simply re-run — the script is idempotent.
#
#  Topology (deterministic — NO proxy_arp):
#    [ISP router 192.168.1.1] keeps WiFi + NAT, DHCP DISABLED
#        └── LAN port ── Ethernet cable ── [old Kali laptop]  (one NIC)
#                 devices join the ROUTER's WiFi, but get their IP from THIS
#                 laptop on a SEPARATE client subnet (192.168.2.0/24,
#                 gateway + DNS = laptop = 192.168.2.1).
#
#  WHY A SEPARATE SUBNET (critical):
#    On a single 192.168.1.0/24 LAN the kernel's proxy_arp REFUSES to answer
#    "who has <device IP>" for same-subnet targets, so the router's return
#    traffic went straight to the device and downloads never crossed this box —
#    no accounting, no cut-off. Giving clients their own 192.168.2.0/24 with
#    masquerade NAT makes EVERY byte cross the laptop deterministically:
#    outbound is routed here (clients' gateway), inbound is NAT'd back to
#    192.168.2.x and the box answers with its own address. The laptop keeps
#    192.168.1.110/24 as its uplink to the router.
#
#  What it does:
#    1. Preflight: root, app not running, wired Ethernet NIC.
#    2. Enables ip_forward, disables IPv6 (persistent sysctl).
#    3. Installs dnsmasq (DHCP + DNS) and nftables.
#    4. Puts 192.168.1.110/24 (uplink) AND 192.168.2.1/24 (clients) on the NIC.
#    5. Writes dnsmasq config: 192.168.2.x pool + gateway + DNS = this laptop.
#    6. Writes the nftables NAT table (`inet quota_nat`) that masquerades the
#       client subnet. The app's accounting/block table (`inet quota_gateway`)
#       is created by run.py itself and NEVER touched here, so re-running this
#       script cannot wipe a live app's rules (old versions flushed everything).
#    7. Writes the app's config + a systemd unit that auto-starts / auto-
#       restarts the gateway.
#
#  Idempotent: safe to re-run after edits. Refuses to run while the app is live.
# ===========================================================================

set -euo pipefail

# --- overridable settings (defaults match config-linux.yaml) -----------------
WAN_GATEWAY="${WAN_GATEWAY:-192.168.1.1}"      # upstream router
LAN_IP="${LAN_IP:-192.168.1.110}"              # this laptop's uplink IP
LAN_CIDR="${LAN_CIDR:-24}"                     # uplink prefix (nmcli wants CIDR)
SUBNET_MASK="${SUBNET_MASK:-255.255.255.0}"
CLIENT_IP="${CLIENT_IP:-192.168.2.1}"          # clients' gateway (laptop alias)
CLIENT_NET="${CLIENT_NET:-192.168.2.0/24}"     # client subnet (for NAT)
POOL_START="${POOL_START:-192.168.2.100}"      # first address handed to devices
POOL_END="${POOL_END:-192.168.2.200}"          # last address handed to devices
UPSTREAM_DNS="${UPSTREAM_DNS:-8.8.8.8}"        # DNS the laptop forwards to
LEASE_HOURS="${LEASE_HOURS:-24}"               # DHCP lease length, hours (fallback-recovery tuning)
# Interface serving the LAN (where devices' traffic enters). Auto-detected
# unless set explicitly. MUST be the wired NIC. Plain 'first default route'
# picks a WiFi NIC or a VPN tun/tap on a laptop with several default routes,
# and a laptop may have two Ethernet ports where only one has a cable. So:
# prefer the kernel's default-route interface, but only if it is an Ethernet
# NIC (type 1) with a live link (carrier); fall back to any wired, cabled
# interface. Override with LAN_IF=ethX when auto-detection still misses.
LAN_IF="${LAN_IF:-}"
if [ -z "$LAN_IF" ]; then
    for cand in $(ip route | awk '/default/ {print $5}') \
                $(ls /sys/class/net | grep -v '^lo$'); do
        [ -d "/sys/class/net/$cand/wireless" ] && continue                # skip WiFi
        [ "$(cat "/sys/class/net/$cand/type" 2>/dev/null || echo 0)" = "1" ] \
            || continue                                                    # Ethernet only
        [ "$(cat "/sys/class/net/$cand/carrier" 2>/dev/null || echo 0)" = "1" ] \
            || continue                                                    # cable present
        LAN_IF="$cand"
        break
    done
fi

CONF_DIR="/etc/quota-gateway"
# The project directory this script lives in (repo root). Used as the app
# location for the systemd unit. Override when the repo is deployed elsewhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"

log()  { echo -e "\e[1;36m[gateway]\e[0m $*"; }
warn() { echo -e "\e[1;33m[gateway]\e[0m $*"; }
die()  { echo -e "\e[1;31m[gateway] $*\e[0m" >&2; exit 1; }

# --- 0. preflight ------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash scripts/setup_gateway_kali.sh)"
pgrep -f "run\.py" >/dev/null 2>&1 && die \
    "the Quota Manager app is RUNNING (run.py). Stop it first (systemctl stop \
quota-gateway) — it owns the live nftables table and this script must not \
reconfigure the network under it."
[ -n "$LAN_IF" ] || die "could not auto-detect a wired (Ethernet) LAN interface \
(candidates: $(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | tr '\n' ' ')). \
Set LAN_IF=ethX explicitly."
log "LAN interface: $LAN_IF"
ip addr show "$LAN_IF" >/dev/null 2>&1 || die "interface $LAN_IF not found"
if [ -d "/sys/class/net/$LAN_IF/wireless" ]; then
    die "LAN_IF=$LAN_IF is a WIRELESS NIC — the gateway must be wired. Set LAN_IF=ethX"
fi
iftype="$(cat "/sys/class/net/$LAN_IF/type" 2>/dev/null || echo 0)"
[ "$iftype" = "1" ] || die \
    "LAN_IF=$LAN_IF is not an Ethernet NIC (type=$iftype) — the gateway must be \
wired. Set LAN_IF=ethX (the auto-detect may have picked a WiFi/VPN default route)"

# Preflight subnet sanity. The defaults assume a 192.168.1.0/24 home LAN; a
# different router LAN (e.g. 192.168.0.1) left unset here silently bricks the
# uplink (laptop sets 192.168.1.110, gateway 192.168.1.1 unreachable) — clients
# get DHCP/DNS but no internet, and the laptop is offline after the router's
# DHCP is disabled. And the client subnet MUST be separate from the uplink.
if [ "$LAN_CIDR" = "24" ] && [ "${WAN_GATEWAY%.*}" != "${LAN_IP%.*}" ]; then
    die "WAN_GATEWAY=$WAN_GATEWAY is not on the same /24 as LAN_IP=$LAN_IP \
(they must share the first three octets, e.g. router 192.168.1.1 + laptop \
192.168.1.110). Set WAN_GATEWAY and/or LAN_IP to match your LAN and re-run."
fi
case "$CLIENT_IP" in
    "${LAN_IP%.*}."*) die "CLIENT_IP=$CLIENT_IP shares the uplink subnet \
${LAN_IP%.*}.0/24 — clients must be on a SEPARATE subnet (default 192.168.2.0/24). \
Set CLIENT_IP and CLIENT_NET to a different /24 and re-run." ;;
esac

# --- 1. kernel forwarding + IPv6 off ----------------------------------------
log "[1/8] enabling ip_forward, disabling IPv6"
mkdir -p /etc/sysctl.d
cat > /etc/sysctl.d/99-quota-gateway.conf <<EOF
# Quota Manager gateway
net.ipv4.ip_forward = 1
# IPv6 off on THIS gateway NIC only. This does NOT stop the ROUTER from
# sending Router Advertisements to WiFi clients — their IPv6 then routes
# through the router and bypasses this box entirely (uncounted, unblockable).
# You MUST also disable IPv6/RA on the router itself; see the NEXT STEPS
# report below. Quota Manager is IPv4-only.
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.$LAN_IF.disable_ipv6 = 1
EOF
sysctl --system >/dev/null 2>&1 || sysctl -p /etc/sysctl.d/99-quota-gateway.conf >/dev/null

# IPv6 on the GATEWAY is disabled above, but clients take IPv6 (RA/DHCPv6)
# directly from the ROUTER when it is dual-stack — that traffic never crosses
# this laptop, so it is UNCOUNTED and UNBLOCKABLE. Nothing in the gateway can
# stop it; the ROUTER's IPv6 (or at least its RA) must be disabled too.
warn "IPv6 note: this gateway only manages IPv4. If your router/ISP is dual-stack,"
warn "clients receive IPv6 straight from the router and that traffic BYPASSES this"
warn "gateway — uncounted and unblockable. On the router, turn off IPv6/DHCPv6/RA"
warn "for the LAN (or accept that only IPv4 traffic is quota-managed)."

# --- 2. install dnsmasq + nftables -------------------------------------------
log "[2/8] installing dnsmasq + nftables"
if command -v apt-get >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq nftables >/dev/null
elif command -v apk >/dev/null; then
    apk add --no-cache dnsmasq nftables
else
    warn "no apt/apk found — install dnsmasq + nftables manually"
fi
systemctl enable dnsmasq >/dev/null 2>&1 || warn "could not enable dnsmasq.service"
if ! systemctl enable nftables >/dev/null 2>&1; then
    die "could not enable nftables.service — the client-subnet NAT will not \
survive a reboot. Fix the nftables package/service, then re-run this script."
fi
# Debian's nftables.service ExecStop is `nft flush ruleset`: a
# `systemctl restart nftables` (e.g. an `apt upgrade` of nftables, or manual
# troubleshooting) would flush the app's live `inet quota_gateway` accounting/
# block table, and the add-only engine never restores it until the app is
# restarted. Scope the stop action to our NAT table only.
mkdir -p /etc/systemd/system/nftables.service.d
cat > /etc/systemd/system/nftables.service.d/override-quota.conf <<'EOF'
[Service]
ExecStop=
ExecStop=/bin/sh -c 'nft flush table inet quota_nat 2>/dev/null || true'
EOF
systemctl daemon-reload

# --- 3. static IPs on the LAN NIC --------------------------------------------
log "[3/8] configuring $LAN_IF = uplink $LAN_IP/$LAN_CIDR + client alias $CLIENT_IP/$LAN_CIDR"
# Use NetworkManager ONLY when it is actually running — the nmcli binary can be
# installed while the daemon is stopped (fresh/minimal Kali, or an ifupdown-
# managed NIC), and then every `nmcli` call fails and `set -e` silently kills
# the script mid-step. Fall back to ifupdown in that case.
if command -v nmcli >/dev/null 2>&1 && nmcli general status >/dev/null 2>&1; then
    # NetworkManager keys connections by PROFILE NAME, not interface — passing
    # the interface name silently configures nothing. Resolve the profile that
    # owns this interface, then set a static primary + a second address for the
    # client subnet. `-ipv4.addresses` first keeps re-runs idempotent.
    profile="$(nmcli -t -f GENERAL.DEVICE,NAME con show 2>/dev/null \
               | awk -F: -v want="$LAN_IF" '$1 == want {print $2; exit}')" || true
    if [ -z "$profile" ]; then
        profile="$(nmcli -t -f NAME,DEVICE con show 2>/dev/null \
                   | awk -F: -v want="$LAN_IF" '$2 == want {print $1; exit}')" || true
    fi
    if [ -n "$profile" ]; then
        nmcli con mod "$profile" ipv4.method manual \
            ipv4.addresses "$LAN_IP/$LAN_CIDR" \
            ipv4.gateway "$WAN_GATEWAY" ipv4.dns "$UPSTREAM_DNS" >/dev/null 2>&1 \
            || warn "nmcli could not set the uplink static IP — set it in NetworkManager GUI"
        nmcli con mod "$profile" -ipv4.addresses "$CLIENT_IP/$LAN_CIDR" >/dev/null 2>&1 || true
        nmcli con mod "$profile" +ipv4.addresses "$CLIENT_IP/$LAN_CIDR" >/dev/null 2>&1 \
            || warn "nmcli could not add the client alias $CLIENT_IP — the quota \
subnet needs it (add it in NetworkManager GUI)"
        nmcli con up "$profile" >/dev/null 2>&1 || true
    else
        # No NetworkManager profile owns this interface (fresh/minimal Kali, or
        # the NIC is unmanaged). The static uplink + the $CLIENT_IP client alias
        # are the CORE of the topology — warn-and-continue here silently leaves
        # the laptop on router DHCP with no alias, and the gateway fails once
        # the router's DHCP is disabled. Create the profile instead.
        if ! nmcli con show "quota-gateway" >/dev/null 2>&1; then
            nmcli con add type ethernet con-name "quota-gateway" ifname "$LAN_IF" \
                ipv4.method manual ipv4.addresses "$LAN_IP/$LAN_CIDR" \
                ipv4.gateway "$WAN_GATEWAY" ipv4.dns "$UPSTREAM_DNS" >/dev/null 2>&1 \
                || die "no NetworkManager profile owns $LAN_IF and 'nmcli con add' \
failed. Create a static ethernet connection for $LAN_IF in the NetworkManager \
GUI (address $LAN_IP/$LAN_CIDR, gateway $WAN_GATEWAY, DNS $UPSTREAM_DNS, plus a \
second address $CLIENT_IP/$LAN_CIDR), then re-run this script."
        fi
        profile="quota-gateway"
        nmcli con mod "$profile" +ipv4.addresses "$CLIENT_IP/$LAN_CIDR" >/dev/null 2>&1 || true
        nmcli con up "$profile" >/dev/null 2>&1 || true
    fi
else
    cat > /etc/network/interfaces.d/quota-gateway <<EOF
auto $LAN_IF
iface $LAN_IF inet static
    address $LAN_IP
    netmask $SUBNET_MASK
    gateway $WAN_GATEWAY
    up ip addr add $CLIENT_IP/$LAN_CIDR dev $LAN_IF
    down ip addr del $CLIENT_IP/$LAN_CIDR dev $LAN_IF
EOF
    warn "NetworkManager not running/usable — wrote /etc/network/interfaces.d/quota-gateway (ifupdown; applies on reboot)"
fi
# Make sure BOTH addresses are live RIGHT NOW (NM may be mid-apply; an ifupdown
# config only takes effect on ifup/reboot). Best-effort, idempotent.
for addr in "$LAN_IP" "$CLIENT_IP"; do
    ip addr show "$LAN_IF" 2>/dev/null | grep -q "inet $addr/" \
        || ip addr add "$addr/$LAN_CIDR" dev "$LAN_IF" 2>/dev/null || true
done
# Verify the static addresses actually landed. A silent failure (NM profile not
# found, NM not running, con up rejected, ifupdown not applied) leaves the
# laptop on router DHCP with NO client alias — the whole topology is dead and
# the failure would only surface after the router's DHCP is disabled.
for expect in "$LAN_IP" "$CLIENT_IP"; do
    ip addr show "$LAN_IF" 2>/dev/null | grep -q "inet $expect/" \
        || die "interface $LAN_IF does not carry $expect — the static-IP \
setup failed. Check the NetworkManager connection for $LAN_IF, set the address \
(and the $CLIENT_IP alias), then re-run this script."
done

# --- 4. dnsmasq: DHCP + DNS forwarder ----------------------------------------
log "[4/8] writing dnsmasq config (DHCP pool + DNS forwarder)"
mkdir -p "$CONF_DIR"
# dnsmasq DIES at startup if it cannot open/create its lease file, and it does
# not mkdir the parent. The Debian package only chowns the file when the
# directory already exists, so guarantee both here — a missing dir means NO
# DHCP and NO DNS on a fresh laptop.
mkdir -p /var/lib/misc
touch /var/lib/misc/dnsmasq.leases 2>/dev/null || true
chown dnsmasq:dnsmasq /var/lib/misc/dnsmasq.leases 2>/dev/null \
    || chown dnsmasq:nogroup /var/lib/misc/dnsmasq.leases 2>/dev/null || true
cat > /etc/dnsmasq.d/quota-gateway.conf <<EOF
# Quota Manager gateway
interface=$LAN_IF
bind-interfaces
# We are the only DHCP server on this L2 (the router's is disabled). Be
# authoritative so a client that reconnects still holding the router's old
# 192.168.1.x lease is NAKed and re-DISCOVERs onto 192.168.2.x immediately
# instead of keeping its bypassing gateway until the old lease expires.
dhcp-authoritative
# DHCP: hand devices IPs on the CLIENT subnet with gateway + DNS = THIS laptop
dhcp-range=$POOL_START,$POOL_END,$SUBNET_MASK,${LEASE_HOURS}h
dhcp-option=3,$CLIENT_IP          # default gateway = the quota laptop
dhcp-option=6,$CLIENT_IP          # DNS = the quota laptop (its dnsmasq forwards)
# DNS: relay upstream (Android uses the gateway as a resolver; answer it).
# Two upstreams for resilience: the router resolves via the ISP's DNS, and
# 8.8.8.8 covers the case where the router's own resolver is flaky. A single
# upstream (8.8.8.8 alone) means one blocked/filtered resolver kills DNS for
# every client while the data path still works.
no-resolv
server=$WAN_GATEWAY
server=$UPSTREAM_DNS
# Log new leases so Quota Manager can learn MAC<->IP bindings
log-dhcp
dhcp-leasefile=/var/lib/misc/dnsmasq.leases
EOF
# dnsmasq.service on Debian/Kali orders only after network.target, which is
# reached before NetworkManager assigns the static uplink ($LAN_IP) and
# client-alias ($CLIENT_IP) addresses. With bind-interfaces above, dnsmasq
# must see those addresses at startup or it fails to bind and exits (no
# Restart=), leaving DHCP+DNS dead until a manual start. Wait for the network
# to be online before starting, like quota-gateway.service does.
mkdir -p /etc/systemd/system/dnsmasq.service.d
cat > /etc/systemd/system/dnsmasq.service.d/network-online.conf <<'EOF'
[Unit]
After=network-online.target
Wants=network-online.target
EOF
systemctl daemon-reload
# Validate before restarting; `set -e` would abort the whole script on a bad
# config, but a broken dnsmasq.conf is a warning, not a reason to stop.
if dnsmasq --test -C /etc/dnsmasq.d/quota-gateway.conf >/dev/null 2>&1; then
    systemctl restart dnsmasq || warn "dnsmasq restart failed — run manually"
else
    warn "dnsmasq config did not validate — fix it before starting the app"
fi

# --- 5. nftables: NAT for the client subnet ----------------------------------
log "[5/8] writing nftables NAT ruleset"
# The app (run.py, quota/nftables.py) owns the `inet quota_gateway` table —
# it flushes and rebuilds it on start, and it MUST NOT be in this file or a
# re-run of setup would fight the live app. This file holds only the NAT
# infrastructure that makes the topology work; it lives in its own table
# (`inet quota_nat`) the app never touches.
cat > "$CONF_DIR/nftables.gateway.nft" <<EOF
#!/usr/sbin/nft -f
# Quota Manager gateway — client-subnet NAT (infrastructure).
# The app adds per-device counters + the 'blocked' set in the SEPARATE table
# 'inet quota_gateway' (q_up_<ip> / q_down_<ip>, dots->underscores). Forwarded
# packets hit that table's forward hook before this postrouting NAT, so the
# counters and the block drops see the real client IPs.
table inet quota_nat {
    chain postrouting {
        type nat hook postrouting priority 100; policy accept;
        # Clients (192.168.2.0/24) exit through this box -> masquerade as the
        # uplink IP so the router answers them.
        ip saddr $CLIENT_NET masquerade
    }
}
EOF
ln -sf "$CONF_DIR/nftables.gateway.nft" /etc/nftables.conf
# Scoped flush ONLY of the NAT table — never `nft flush ruleset`, which would
# wipe a live app's accounting/block table.
nft flush table inet quota_nat 2>/dev/null || true   # table may not exist yet
nft -f /etc/nftables.conf
log "   NAT active: $CLIENT_NET -> masquerade via $LAN_IP"

# --- 6. writable app dirs + example config -----------------------------------
log "[6/8] preparing app directories + config"
mkdir -p /var/lib/quota-gateway /var/log/quota-gateway "$CONF_DIR"
cat > "$CONF_DIR/config-linux.yaml" <<EOF
bundle:
  total_gb: 140.0
  reset_day: 1
db_path: /var/lib/quota-gateway/quota.db
log_file: /var/log/quota-gateway/quota.log
dhcp:
  enable: true
  gateway_ip: $CLIENT_IP
  router_ip: $WAN_GATEWAY
  dns_servers: [$WAN_GATEWAY, $UPSTREAM_DNS]
  dns_forward: true
  subnet: $SUBNET_MASK
  pool_start: $POOL_START
  pool_end: $POOL_END
  lease_file: /var/lib/misc/dnsmasq.leases
  lease_hours: $LEASE_HOURS
engine:
  enabled: true
  backend: nftables
  table: quota_gateway
arp:
  enabled: false
web:
  host: 0.0.0.0
  port: 8080
EOF
echo "  example config written to $CONF_DIR/config-linux.yaml"

# --- 7. systemd unit (auto-start + auto-restart) -----------------------------
log "[7/8] writing systemd unit"
# Pick the interpreter: prefer a project venv if one exists, else system python3.
if [ -x "$APP_DIR/.venv/bin/python3" ]; then
    PYTHON="$APP_DIR/.venv/bin/python3"
elif [ -x "$APP_DIR/.venv/bin/python" ]; then
    PYTHON="$APP_DIR/.venv/bin/python"
else
    PYTHON="$(command -v python3 || echo /usr/bin/python3)"
fi
cat > /etc/systemd/system/quota-gateway.service <<EOF
[Unit]
Description=Quota Manager gateway (accounting + quota enforcement)
After=network-online.target nftables.service
Wants=network-online.target

[Service]
Type=simple
# Restart on ANY crash — a 24/7 gateway that silently dies leaves every device
# unmanaged until someone comes home.
Restart=always
RestartSec=5
ExecStart=$PYTHON $APP_DIR/run.py --config $CONF_DIR/config-linux.yaml
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable quota-gateway >/dev/null 2>&1 || warn "could not enable quota-gateway.service"

# --- 8. info report -----------------------------------------------------------
log "[8/8] done. Info report:"
echo "  uplink IP     : $LAN_IP (via router $WAN_GATEWAY)"
echo "  client subnet : $CLIENT_NET, gateway/DNS = $CLIENT_IP"
echo "  DHCP pool     : $POOL_START - $POOL_END"
echo "  DNS           : laptop dnsmasq -> $WAN_GATEWAY + $UPSTREAM_DNS"
echo "  LAN interface : $LAN_IF (wired)"
echo
echo "  NEXT STEPS (do the venv FIRST — it must exist before (re)running this"
echo "  script so the systemd unit points at .venv/bin/python3):"
echo "   1) Create the venv + install the Python deps:"
echo "        cd $APP_DIR"
echo "        python3 -m venv .venv && .venv/bin/pip install -r requirements-linux.txt"
echo "      If you created the venv AFTER running this script, re-run it now"
echo "      (it is idempotent) so the unit picks up the venv interpreter."
echo "   2) On the ROUTER: disable its DHCP server (leave WiFi + NAT on)."
echo "      ALSO on the ROUTER: turn OFF IPv6 / Router Advertisement (RA) on"
echo "      the WiFi + LAN. Quota Manager counts and blocks IPv4 ONLY; the"
echo "      sysctl above only disables IPv6 on THIS laptop, which does NOT"
echo "      stop the router handing IPv6 to WiFi clients. If the router/ISP is"
echo "      dual-stack, client IPv6 traffic goes client->router->ISP and NEVER"
echo "      crosses this gateway — it is uncounted and unblockable. If your"
echo "      router cannot disable IPv6, accept that IPv6-using apps bypass the"
echo "      quota."
echo "      Optional electric-cut fallback: give the ROUTER a small DHCP pool"
echo "      OUTSIDE the client subnet (e.g. 192.168.1.201-250, gateway=router)"
echo "      so devices stay online while this laptop is down."
echo "   3) Start the gateway:"
echo "        systemctl start quota-gateway"
echo "   4) Watch it come up:  journalctl -u quota-gateway -f"
echo "      Dashboard:  http://$CLIENT_IP:8080  (default password 'admin',"
echo "      change it in Settings)."
echo
echo "  Re-run this script anytime to re-apply settings (it stops first if the"
echo "  app is running)."
