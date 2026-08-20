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

# --- overridable settings (defaults match config.yaml) ------------------------
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
# Deployment topology. lan (default): the box sits behind the router (clients on
# their own subnet, router keeps WiFi + NAT). wan ("strong" mode): the box dials
# PPPoE itself (public IP on ppp0) and the router is a pure bridge/AP, so a
# static-IP device has NO second router to bypass through. The dashboard WAN tab
# can also toggle this (applies on the next restart).
WAN_IF="${WAN_IF:-}"                 # two-NIC WAN layout: the NIC that reaches the ONT/modem
PPPOE_USER="${PPPOE_USER:-}"         # WAN mode: PPPoE login (ISP username)
PPPOE_PASSWORD="${PPPOE_PASSWORD:-}" # WAN mode: PPPoE password
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
# Bundle / timezone / admin password — install.sh prompts for these, but this
# script is also run directly. Defaults come from the CURRENT /etc config if one
# exists: re-running must NEVER clobber a real bundle (e.g. 50 GB / day 15) back
# to a hardcoded 140/1 — that guarantee is what makes the update path safe.
existing_gb=140.0; existing_day=1; existing_tz=""; existing_topology=""
_CFG="$CONF_DIR/config.yaml"
if [ -f "$_CFG" ]; then
    existing_gb="$(awk '/^[[:space:]]*total_gb:/{gsub(/[^0-9.]/,"",$2); print $2; exit}' "$_CFG")"
    existing_day="$(awk '/^[[:space:]]*reset_day:/{print $2; exit}' "$_CFG")"
    existing_tz="$(awk '/^[[:space:]]*timezone:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "$_CFG")"
    existing_topology="$(awk '/^[[:space:]]*topology:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "$_CFG")"
fi
BUNDLE_TOTAL_GB="${BUNDLE_TOTAL_GB:-${existing_gb:-140.0}}"
BUNDLE_RESET_DAY="${BUNDLE_RESET_DAY:-${existing_day:-1}}"
TIMEZONE="${TIMEZONE:-${existing_tz:-}}"
QUOTA_TOPOLOGY="${QUOTA_TOPOLOGY:-${existing_topology:-lan}}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
# Validate — a bad env value must fail loudly, not be written silently.
case "$BUNDLE_TOTAL_GB" in
    ''|*[!0-9.]*|*.*.*) die "BUNDLE_TOTAL_GB='$BUNDLE_TOTAL_GB' is not a number (e.g. 140)" ;;
esac
awk -v g="$BUNDLE_TOTAL_GB" 'BEGIN { exit (g <= 0) ? 1 : 0 }' \
    || die "BUNDLE_TOTAL_GB='$BUNDLE_TOTAL_GB' must be greater than 0"
case "$BUNDLE_RESET_DAY" in
    ''|*[!0-9]*) die "BUNDLE_RESET_DAY='$BUNDLE_RESET_DAY' must be 0-28 (0 = no auto-reset)" ;;
esac
[ "$BUNDLE_RESET_DAY" -le 28 ] || die "BUNDLE_RESET_DAY='$BUNDLE_RESET_DAY' must be 0-28 (0 = no auto-reset)"
if [ -n "$ADMIN_PASSWORD" ]; then
    # A bare `\` is an escape char inside a case pattern, so use [[ ]] with
    # single-quoted chars to match a literal backslash.
    if [[ "$ADMIN_PASSWORD" == *'"'* ]] || [[ "$ADMIN_PASSWORD" == *'\'* ]]; then
        die "ADMIN_PASSWORD must not contain a double-quote or backslash \
(systemd Environment= parsing)"
    fi
fi
case "$QUOTA_TOPOLOGY" in
    lan|wan) ;;
    *) die "QUOTA_TOPOLOGY='$QUOTA_TOPOLOGY' must be 'lan' (default: box behind \
the router) or 'wan' (box dials PPPoE, router becomes a bridge/AP)" ;;
esac

# The project directory this script lives in (repo root). Used as the app
# location for the systemd unit. Override when the repo is deployed elsewhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"

log()  { echo -e "\e[1;36m[gateway]\e[0m $*"; }
warn() { echo -e "\e[1;33m[gateway]\e[0m $*"; }
die()  { echo -e "\e[1;31m[gateway] $*\e[0m" >&2; exit 1; }
wan_mode() { [ "$QUOTA_TOPOLOGY" = "wan" ]; }
# The NIC that dials PPPoE in WAN mode: the two-NIC layout's WAN_IF when set,
# otherwise the single-NIC layout reuses LAN_IF (which also carries the client
# subnet — PPPoE frames and the client alias coexist on one physical NIC).
PPP_IF="${WAN_IF:-$LAN_IF}"

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
if wan_mode && [ -n "$WAN_IF" ]; then
    ip link show "$WAN_IF" >/dev/null 2>&1 \
        || die "WAN_IF=$WAN_IF not found (two-NIC WAN layout: the NIC that reaches \
the ONT/modem and dials PPPoE). Set WAN_IF=ethX and LAN_IF to the client-facing NIC."
fi

# Preflight subnet sanity. The defaults assume a 192.168.1.0/24 home LAN; a
# different router LAN (e.g. 192.168.0.1) left unset here silently bricks the
# uplink (laptop sets 192.168.1.110, gateway 192.168.1.1 unreachable) — clients
# get DHCP/DNS but no internet, and the laptop is offline after the router's
# DHCP is disabled. And the client subnet MUST be separate from the uplink.
# WAN mode has no uplink LAN (pppd owns the WAN), so these checks do not apply.
if ! wan_mode; then
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
fi

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

# --- 2. install dnsmasq + nftables + iproute2 ---------------------------------
# The QUOTA_NO_APT=1 guard: when this script runs from the package's postinst,
# the dependencies are already satisfied by the .deb's Depends field, so the
# apt block is skipped (and the postinst therefore runs offline-safe).
log "[2/8] installing dnsmasq + nftables + iproute2"
if [ -n "${QUOTA_NO_APT:-}" ]; then
    log "   QUOTA_NO_APT is set (package install) — assuming dnsmasq + nftables + iproute2 are installed"
elif command -v apt-get >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq nftables iproute2 >/dev/null
elif command -v apk >/dev/null; then
    apk add --no-cache dnsmasq nftables iproute2
else
    warn "no apt/apk found — install dnsmasq + nftables + iproute2 manually"
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
# WAN mode: the box terminates the WAN itself (pppd dials PPPoE on $PPP_IF), so
# $LAN_IF carries the client subnet as its PRIMARY address — plus the old uplink
# IP kept as a SECONDARY alias so clients can still reach the router's admin
# page (the router is bridged; the box's connected route carries the traffic).
# No gateway, no upstream DNS: the router is a pure bridge/AP and hands out no
# addresses, and pppd owns the WAN.
if wan_mode; then
    log "[3/8] WAN mode: $LAN_IF = client subnet $CLIENT_IP/$LAN_CIDR + router-admin alias $LAN_IP/$LAN_CIDR"
    if command -v nmcli >/dev/null 2>&1 && nmcli general status >/dev/null 2>&1; then
        # NetworkManager keys connections by PROFILE NAME, not interface.
        profile="$(nmcli -t -f GENERAL.DEVICE,NAME con show 2>/dev/null \
                   | awk -F: -v want="$LAN_IF" '$1 == want {print $2; exit}')" || true
        if [ -z "$profile" ]; then
            profile="$(nmcli -t -f NAME,DEVICE con show 2>/dev/null \
                       | awk -F: -v want="$LAN_IF" '$2 == want {print $1; exit}')" || true
        fi
        if [ -n "$profile" ]; then
            # Setting ipv4.addresses to ONE value replaces the whole list, so a
            # previous LAN-mode profile (uplink + alias) converges on re-run.
            nmcli con mod "$profile" ipv4.method manual \
                ipv4.addresses "$CLIENT_IP/$LAN_CIDR" \
                ipv4.gateway "" ipv4.dns "" >/dev/null 2>&1 \
                || warn "nmcli could not set the client subnet — set it in NetworkManager GUI"
            # Keep the uplink IP as a SECONDARY address: the router is bridged
            # in WAN mode, so clients reach its admin page through the box's
            # connected route — automatic router access (v19.5).
            nmcli con mod "$profile" -ipv4.addresses "$LAN_IP/$LAN_CIDR" >/dev/null 2>&1 || true
            nmcli con mod "$profile" +ipv4.addresses "$LAN_IP/$LAN_CIDR" >/dev/null 2>&1 \
                || warn "nmcli could not add the router-admin alias $LAN_IP — the \
router admin page (${WAN_GATEWAY:-192.168.1.1}) is unreachable from clients"
            nmcli con up "$profile" >/dev/null 2>&1 || true
        else
            if ! nmcli con show "quota-gateway" >/dev/null 2>&1; then
                nmcli con add type ethernet con-name "quota-gateway" ifname "$LAN_IF" \
                    ipv4.method manual ipv4.addresses "$CLIENT_IP/$LAN_CIDR" >/dev/null 2>&1 \
                    || die "no NetworkManager profile owns $LAN_IF and 'nmcli con add' \
failed. Create a static ethernet connection for $LAN_IF in the NetworkManager \
GUI (address $CLIENT_IP/$LAN_CIDR), then re-run this script."
            fi
            profile="quota-gateway"
            nmcli con mod "$profile" ipv4.method manual \
                ipv4.addresses "$CLIENT_IP/$LAN_CIDR" \
                ipv4.gateway "" ipv4.dns "" >/dev/null 2>&1 || true
            nmcli con mod "$profile" -ipv4.addresses "$LAN_IP/$LAN_CIDR" >/dev/null 2>&1 || true
            nmcli con mod "$profile" +ipv4.addresses "$LAN_IP/$LAN_CIDR" >/dev/null 2>&1 || true
            nmcli con up "$profile" >/dev/null 2>&1 || true
        fi
    else
        cat > /etc/network/interfaces.d/quota-gateway <<EOF
auto $LAN_IF
iface $LAN_IF inet static
    address $CLIENT_IP
    netmask $SUBNET_MASK
    up ip addr add $LAN_IP/$LAN_CIDR dev $LAN_IF
    down ip addr del $LAN_IP/$LAN_CIDR dev $LAN_IF
EOF
        warn "NetworkManager not running/usable — wrote /etc/network/interfaces.d/quota-gateway (ifupdown; applies on reboot)"
    fi
    # Make sure both addresses are live RIGHT NOW (NM may be mid-apply; an
    # ifupdown config only takes effect on ifup/reboot). Best-effort, idempotent.
    for addr in "$CLIENT_IP" "$LAN_IP"; do
        ip addr show "$LAN_IF" 2>/dev/null | grep -q "inet $addr/" \
            || ip addr add "$addr/$LAN_CIDR" dev "$LAN_IF" 2>/dev/null || true
    done
    # Verify the client subnet actually landed — the WAN topology is dead
    # without it (clients have no gateway to the box).
    ip addr show "$LAN_IF" 2>/dev/null | grep -q "inet $CLIENT_IP/" \
        || die "interface $LAN_IF does not carry $CLIENT_IP — the WAN-mode static-IP \
setup failed. Set the $CLIENT_IP alias on $LAN_IF (NetworkManager GUI), then re-run."
else
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
fi

# --- 4. dnsmasq: DHCP + DNS forwarder ----------------------------------------
log "[4/8] writing dnsmasq config (DHCP pool + DNS forwarder)"
mkdir -p "$CONF_DIR"
# dnsmasq only reads /etc/dnsmasq.d if the main config includes it
# (conf-dir=...). Debian/Kali ship it uncommented, but a stripped or hand-edited
# /etc/dnsmasq.conf may have it commented or missing — and then dnsmasq silently
# ignores EVERY quota fragment below (DHCP pool, DNS settings, the query log)
# while still starting cleanly. Uncomment an existing line or append one so the
# fragments actually load. Idempotent: an already-active conf-dir is untouched.
if ! grep -qE '^\s*conf-dir=' /etc/dnsmasq.conf; then
    if grep -qE '^\s*#\s*conf-dir=' /etc/dnsmasq.conf; then
        sed -i 's|^\s*#\s*conf-dir=|conf-dir=|' /etc/dnsmasq.conf
        log "   enabled conf-dir in /etc/dnsmasq.conf (was commented)"
    else
        echo 'conf-dir=/etc/dnsmasq.d,.dpkg-dist,.dpkg-old,.dpkg-new' >> /etc/dnsmasq.conf
        log "   appended conf-dir=/etc/dnsmasq.d to /etc/dnsmasq.conf"
    fi
fi
# dnsmasq DIES at startup if it cannot open/create its lease file, and it does
# not mkdir the parent. The Debian package only chowns the file when the
# directory already exists, so guarantee both here — a missing dir means NO
# DHCP and NO DNS on a fresh laptop.
mkdir -p /var/lib/misc
touch /var/lib/misc/dnsmasq.leases 2>/dev/null || true
chown dnsmasq:dnsmasq /var/lib/misc/dnsmasq.leases 2>/dev/null \
    || chown dnsmasq:nogroup /var/lib/misc/dnsmasq.leases 2>/dev/null || true
if wan_mode; then
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
else
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
# dhcp-sequential-ip: allocate STRICTLY in order from POOL_START (the dnsmasq
# default hashes by MAC across the whole pool -> gapped leases like .155/.185)
dhcp-sequential-ip
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
fi
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

# --- 4.5. dnsmasq query log (per-device browsing history) ---------------------
# App-owned fragment the setup/topology scripts never rewrite (they only touch
# quota-gateway.conf), so a LAN/WAN toggle keeps history logging. log-queries=extra
# puts the requestor IP on every line; log-async bounds DNS latency; the app's
# DnslogTailer reads log-facility and logrotate bounds the raw file.
# (Defined HERE, before the fragment is rendered — the config.yaml heredoc in
# step 6 re-uses the same value, but with `set -u` a late assignment crashed
# installs with "CFG_HISTORY_LOG: unbound variable" at this heredoc.)
CFG_HISTORY_LOG="${CFG_HISTORY_LOG:-/var/log/quota-dnsmasq.log}"
mkdir -p "$CONF_DIR"
cat > /etc/dnsmasq.d/quota-dnslog.conf <<EOF
# Quota Manager — per-device browsing history (tailer: quota/dnslog.py)
# Setup owns this file; quota-gateway.conf is the only one the scripts rewrite.
log-queries=extra
log-async=20
log-facility=$CFG_HISTORY_LOG
EOF
# The tailer tolerates a missing file, but dnsmasq chowning it here means the
# app can read it immediately (dnsmasq runs as dnsmasq, the app as root).
mkdir -p "$(dirname "$CFG_HISTORY_LOG")"
touch "$CFG_HISTORY_LOG" 2>/dev/null || true
chown dnsmasq:dnsmasq "$CFG_HISTORY_LOG" 2>/dev/null \
    || chown dnsmasq:nogroup "$CFG_HISTORY_LOG" 2>/dev/null || true
# logrotate bounds the raw file even if the app is down (copytruncate keeps
# dnsmasq's open fd valid; the tailer detects the truncation via size shrink).
cat > /etc/logrotate.d/quota-dnsmasq <<EOF
$CFG_HISTORY_LOG {
    daily
    size 5M
    rotate 3
    missingok
    notifempty
    compress
    copytruncate
}
EOF
if dnsmasq --test -C /etc/dnsmasq.d/quota-dnslog.conf >/dev/null 2>&1; then
    systemctl restart dnsmasq || warn "dnsmasq restart failed — run manually"
else
    warn "dnsmasq query-log fragment did not validate — browsing history will be empty"
fi

# --- 4.6. dnsmasq: domain-filtering base files (blacklists/DNS overrides) ---
# quota/dns_rules.py (the DNS-filtering feature) writes generated rules into
# these two files whenever the admin edits a domain rule / preset / per-client
# DNS server in the dashboard. They start EMPTY here — the app owns their
# content from the first rule onward. conf-dir is already guaranteed active
# by step 4 above, so this step is just the placeholder files, nothing else.
log "[4.6/8] preparing dnsmasq domain-filtering files (empty until rules are added)"
for f in quota-tags.conf quota-domains.conf; do
    [ -f "/etc/dnsmasq.d/$f" ] || printf '# Quota Manager — generated, do not edit by hand.\n' \
        > "/etc/dnsmasq.d/$f"
done

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
if wan_mode; then
    log "   NAT active: $CLIENT_NET -> masquerade out $PPP_IF (ppp0 = public IP)"
else
    log "   NAT active: $CLIENT_NET -> masquerade via $LAN_IP"
fi

# --- 5.5. ifb (Intermediate Functional Block) for speed shaping ----------------
# Speed shaping (tc) redirects uploads into an ifb device so they can be shaped
# by pre-NAT source IP; without the module the shaper degrades gracefully (no
# limits, no AQM) but it is one syscall, so load it now and persist it.
log "[5.5/8] loading ifb for tc speed shaping"
if modprobe ifb numifbs=1 2>/dev/null; then
    ip link set up dev ifb0 2>/dev/null || true
    echo ifb > /etc/modules-load.d/quota-gateway.conf
    log "   ifb loaded (ifb0) + persisted via /etc/modules-load.d/quota-gateway.conf"
else
    warn "modprobe ifb failed — per-device / per-user speed limits + low-latency \
queues will be unavailable (quota blocks and accounting still work). Install \
'kmod' (modprobe) or load the module manually and re-run."
fi

# --- 5.6. (WAN mode only) PPPoE bring-up --------------------------------------
# WAN mode terminates the metered line on this box: pppd dials PPPoE on $PPP_IF
# (=$WAN_IF for the two-NIC layout, =$LAN_IF for the single-NIC bridged-router
# layout) and the public IP lands on ppp0. Placed AFTER step 5.5 on purpose:
# step 5.5 owns /etc/modules-load.d/quota-gateway.conf (it overwrites the file
# with `echo ifb >`), so pppoe's module line is appended here to survive that
# overwrite. The NAT rule from step 5 masquerades clients out ppp0 unchanged.
if wan_mode; then
    log "[5.6/8] WAN mode: PPPoE bring-up ($PPP_IF -> ppp0)"
    # The ppp package (pppd + pppoe plugin). The QUOTA_NO_APT guard mirrors
    # step 2: when running from the package postinst, the .deb's Depends field
    # carries 'ppp', so apt is skipped.
    if [ -n "${QUOTA_NO_APT:-}" ]; then
        command -v pppd >/dev/null 2>&1 || warn "QUOTA_NO_APT is set but pppd is \
not installed — WAN mode cannot dial. Install the 'ppp' package, then re-run."
    elif command -v apt-get >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ppp >/dev/null 2>&1 \
            || warn "apt install ppp failed — WAN mode cannot dial until it is installed"
    else
        warn "no apt/apk found — install the 'ppp' package manually"
    fi
    # pppoe kernel module + persist across reboots.
    if modprobe pppoe 2>/dev/null; then
        if ! grep -qx 'pppoe' /etc/modules-load.d/quota-gateway.conf 2>/dev/null; then
            echo pppoe >> /etc/modules-load.d/quota-gateway.conf 2>/dev/null || true
        fi
        log "   pppoe module loaded + persisted"
    else
        warn "modprobe pppoe failed — pppd may auto-load it anyway; install 'kmod' if dialing fails"
    fi
    # The peer + secrets. Credentials go in chap-secrets/pap-secrets (chmod 600),
    # NOT in the peer file, so the ISP password is never world-readable.
    # `noauth` (below) is REQUIRED: pppd enables `auth` (require the PEER to
    # authenticate) by default whenever a `user` + secrets file are present, and
    # the BRAS refuses to authenticate to the client ("peer refused to
    # authenticate: terminating link"). noauth keeps the client-side PAP/CHAP.
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
        warn "PPPOE_USER/PPPOE_PASSWORD not set — dialing with 'noauth', which most \
ISP lines reject. Set both and re-run to dial properly."
    fi
    # The systemd unit: auto-redial at boot + after a crash. pppd itself retries
    # forever (persist + maxfail 0); Restart=always is a safety net.
    if command -v pppd >/dev/null 2>&1 || [ -x /usr/sbin/pppd ]; then
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
        systemctl restart quota-wan-ppp 2>/dev/null \
            || warn "could not start quota-wan-ppp.service — check 'journalctl -u quota-wan-ppp -f'"
        log "   quota-wan-ppp.service enabled + started (ppp0 = public IP)"
    else
        warn "pppd not found — skipping quota-wan-ppp.service. Install the 'ppp' package and re-run."
    fi
else
    # LAN mode (including a WAN -> LAN re-run): the dial MUST be torn down.
    # v18 bug: WAN mode enabled + started quota-wan-ppp but there was NO
    # disable path, so a LAN re-run left pppd (persist + replacedefaultroute)
    # stealing the default route — the box lost its uplink. Disable the
    # service, kill any stray pppd, and drop whatever ppp0 left behind.
    if [ -e /etc/systemd/system/quota-wan-ppp.service ]; then
        log "   LAN mode: disabling + stopping quota-wan-ppp (WAN dial teardown)"
        systemctl disable quota-wan-ppp >/dev/null 2>&1 || true
        systemctl stop quota-wan-ppp >/dev/null 2>&1 || true
    fi
    pkill -f "pppd call quota-wan" >/dev/null 2>&1 || true
    ip addr flush dev ppp0 2>/dev/null || true
    ip link set ppp0 down 2>/dev/null || true
    ip route flush dev ppp0 2>/dev/null || true
fi

# --- 6. writable app dirs + example config -----------------------------------
log "[6/8] preparing app directories + config"
mkdir -p /var/lib/quota-gateway /var/log/quota-gateway "$CONF_DIR"
# The uplink LAN subnet (network of LAN_IP/LAN_CIDR, e.g. 192.168.1.110/24 ->
# 192.168.1.0/24). The engine excludes it from accounting: local traffic
# (router admin, NAS, router-as-DNS) must NOT consume the metered bundle.
_ip_net_of() {
    local ip="$1" cidr="$2"
    local IFS=. a b c d mask o1 o2 o3 o4
    read -r a b c d <<<"$ip"
    mask=$(( 0xFFFFFFFF << (32 - cidr) & 0xFFFFFFFF ))
    o1=$(( (mask >> 24) & 0xFF )); o2=$(( (mask >> 16) & 0xFF ))
    o3=$(( (mask >> 8) & 0xFF ));  o4=$(( mask & 0xFF ))
    echo "$((a & o1)).$((b & o2)).$((c & o3)).$((d & o4))/$cidr"
}
LAN_NET="$(_ip_net_of "$LAN_IP" "$LAN_CIDR")"
# WAN mode rewrites the engine/dhcp blocks: no router on the LAN, no uplink LAN
# to exclude from accounting, no ARP gateway-lock target. `topology` tells the
# engine to treat the box as the WAN terminator (client subnet only).
if wan_mode; then
    CFG_ROUTER_IP='""'
    CFG_DNS_SERVERS="[$UPSTREAM_DNS]"
    CFG_UPLINK_SUBNET='""'
    CFG_ARP_LOCK="false"
    CFG_TOPOLOGY="wan"
else
    CFG_ROUTER_IP="$WAN_GATEWAY"
    CFG_DNS_SERVERS="[$WAN_GATEWAY, $UPSTREAM_DNS]"
    CFG_UPLINK_SUBNET="$LAN_NET"
    CFG_ARP_LOCK="true"
    CFG_TOPOLOGY="lan"
fi
cat > "$CONF_DIR/config.yaml" <<EOF
bundle:
  total_gb: $BUNDLE_TOTAL_GB
  reset_day: $BUNDLE_RESET_DAY
db_path: /var/lib/quota-gateway/quota.db
log_file: /var/log/quota-gateway/quota.log
timezone: $TIMEZONE
dhcp:
  enable: true
  gateway_ip: $CLIENT_IP
  router_ip: $CFG_ROUTER_IP
  dns_servers: $CFG_DNS_SERVERS
  dns_forward: true
  subnet: $SUBNET_MASK
  pool_start: $POOL_START
  pool_end: $POOL_END
  lease_file: /var/lib/misc/dnsmasq.leases
  lease_hours: $LEASE_HOURS
  # LAN-reality snapshot: WAN mode erases the ACTIVE router/dns keys above, but
  # these remember the router-side values in BOTH topologies so the dashboard
  # WAN tab's "Revert to LAN" can restore exactly what was there (v19).
  lan_router_ip: $WAN_GATEWAY
  lan_dns_servers: [$WAN_GATEWAY, $UPSTREAM_DNS]
  uplink_ip: $LAN_IP
  lan_cidr: $LAN_CIDR
engine:
  enabled: true
  backend: nftables
  table: quota_gateway
  # LOCAL (LAN) traffic never consumes the bundle: same-subnet clients and the
  # uplink LAN (router admin, NAS) are excluded from accounting + block drops.
  client_subnet: $CLIENT_NET
  uplink_subnet: $CFG_UPLINK_SUBNET
  # Deployment topology: lan = box behind the router (default); wan = the box
  # terminates the PPPoE line itself (router is a bridge/AP — no bypass target).
  topology: $CFG_TOPOLOGY
  # ARP gateway-lock: actively DENY internet to any device that bypasses the
  # box by using the ROUTER as its gateway (static-IP cheat). The box captures
  # the router's IP on the client subnet and drops the bypasser's frames — its
  # internet is cut until it uses $CLIENT_IP instead. WAN mode has no router on
  # the LAN, so the lock is off by construction.
  gateway_arp_lock: $CFG_ARP_LOCK
  # LAN-reality ARP-lock value: the setup enables the lock in LAN mode, so the
  # WAN tab's Revert restores true (the active key above flips to false in WAN).
  lan_gateway_arp_lock: true
# Speed shaping (tc/HTB + fq_codel): per-device + per-user internet speed caps
# and low-latency (bufferbloat-free) queues. The dashboard Network tab stores
# the runtime totals/caps in the DB; this block only picks the NIC to shape on.
shaping:
  enabled: true
  interface: $LAN_IF
  client_subnet: $CLIENT_NET
  # LAN (client <-> router-LAN) traffic rides a pass-through class at this
  # rate; only internet-bound bytes are shaped at the line cap below.
  lan_rate_mbps: 1000
  ifb: ifb0
# Per-device browsing history (dnsmasq query-log tailer, quota/dnslog.py).
# enabled: false stops the app reading the log entirely (DNS/DHCP unaffected).
history:
  enabled: true
  dnsmasq_log_file: $CFG_HISTORY_LOG
  retention_days: 7
web:
  host: 0.0.0.0
  port: 8080
EOF
echo "  example config written to $CONF_DIR/config.yaml"
# Security hardening: the config holds PPPoE credentials — root-only read,
# never world-readable. The DB holds password hashes + settings; same story.
chmod 600 "$CONF_DIR/config.yaml"
if [ -f /var/lib/quota-gateway/quota.db ]; then
    chmod 600 /var/lib/quota-gateway/quota.db
fi
# A setup re-run is the admin's authoritative "set the box to THIS topology",
# so drop any dashboard-persisted override that would otherwise re-force the
# old value on the next boot (the v18 revert bug: setup rewrote config.yaml to
# `topology: lan` but the DB still held topology_source=dashboard + topology=wan,
# so the app kept forcing WAN). v19's WAN tab writes config.yaml + the DB
# together, so this is only the safety net for manual setup runs.
if [ -f /var/lib/quota-gateway/quota.db ]; then
    if PY="$(command -v python3 || true)" && [ -n "$PY" ]; then
        if "$PY" -c '
import sqlite3, sys
db = sqlite3.connect("/var/lib/quota-gateway/quota.db")
try:
    db.execute("DELETE FROM settings WHERE key IN (\"topology\", \"topology_source\")")
    db.commit()
except sqlite3.Error as e:
    print("could not clear topology override:", e, file=sys.stderr)
' >/dev/null 2>&1; then
            log "   cleared dashboard topology override from quota.db"
        else
            warn "could not clear the dashboard topology override (python3/DB issue) — the WAN tab may fight setup"
        fi
    fi
fi

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
# Seed the initial admin password on FIRST boot only: api/app.py
# (_ensure_admin_password) reads QUOTA_ADMIN_PASSWORD when no password row
# exists. Once seeded it stays in the DB, so on later re-runs the env line
# disappearing is harmless. systemd unit files default to world-readable 0644,
# so when a password is embedded we chmod the unit to root-only.
ADMIN_ENV_LINE=""
if [ -n "$ADMIN_PASSWORD" ]; then
    ADMIN_ENV_LINE="Environment=\"QUOTA_ADMIN_PASSWORD=$ADMIN_PASSWORD\""
fi
# WAN mode: the app starts after the PPPoE link so ppp0 (and its default route)
# is up before the engine's forward-path rules come online.
AFTER_DEP="network-online.target nftables.service"
if wan_mode; then
    AFTER_DEP="$AFTER_DEP quota-wan-ppp.service"
fi
cat > /etc/systemd/system/quota-gateway.service <<EOF
[Unit]
Description=Quota Manager gateway (accounting + quota enforcement)
After=$AFTER_DEP
Wants=network-online.target

[Service]
Type=simple
# Restart on ANY crash — a 24/7 gateway that silently dies leaves every device
# unmanaged until someone comes home.
Restart=always
RestartSec=5
ExecStart=$PYTHON $APP_DIR/run.py --config $CONF_DIR/config.yaml
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
${ADMIN_ENV_LINE}

[Install]
WantedBy=multi-user.target
EOF
if [ -n "$ADMIN_PASSWORD" ]; then
    chmod 600 /etc/systemd/system/quota-gateway.service
    log "   admin password seeded (systemd Environment, unit chmod 600)"
fi
systemctl daemon-reload
systemctl enable quota-gateway >/dev/null 2>&1 || warn "could not enable quota-gateway.service"

# --- 8. info report -----------------------------------------------------------
log "[8/8] done. Info report:"
if wan_mode; then
    echo "  topology       : WAN mode — the box dials PPPoE (router = bridge/AP)"
    echo "  ppp dials      : $PPP_IF -> ppp0 (public IP), quota-wan-ppp.service"
    echo "  client subnet  : $CLIENT_NET, gateway/DNS = $CLIENT_IP"
    echo "  DHCP pool      : $POOL_START - $POOL_END"
    echo "  DNS            : laptop dnsmasq -> $UPSTREAM_DNS"
    echo "  LAN interface  : $LAN_IF (wired, client-facing)"
    echo
    echo "  NEXT STEPS (WAN mode — do the venv FIRST, exactly as in the LAN report:"
    echo "  cd $APP_DIR && python3 -m venv .venv && .venv/bin/pip install -r"
    echo "  requirements-linux.txt, then re-run this script):"
    echo "   1) Put the ROUTER in BRIDGE/MODEM mode (WAN<->LAN bridged, NAT + DHCP"
    echo "      OFF, WiFi on). Single-NIC layout: the box's one cable goes to a"
    echo "      router LAN port. Two-NIC fallback: $WAN_IF goes straight to the"
    echo "      ONT/fiber (or a bridge-mode DSL modem) and $LAN_IF to the router"
    echo "      set to AP mode (WiFi only, DHCP off). AP mode works on EVERY"
    echo "      router, so the two-NIC path is the universal one — it needs a USB"
    echo "      Ethernet dongle as the second NIC, and you must set LAN_IF and"
    echo "      WAN_IF explicitly (the auto-detect may pick the wrong wired NIC)."
    echo "      Bridge-mode hints (WE ZTE/Huawei, Orange Livebox, Vodafone, e&):"
    echo "      look for 'Bridge', 'Modem' or 'WAN mode' in the router's WAN"
    echo "      settings. ISP-locked combos may need a bridge-unlock code or an"
    echo "      ISP call."
    echo "   2) PPPoE credentials: from your ISP card or the router's WAN status"
    echo "      page (the username/password the router dialed with). Re-run setup"
    echo "      with them:"
    echo "        QUOTA_TOPOLOGY=wan PPPOE_USER=... PPPOE_PASSWORD=... \\"
    echo "          scripts/setup_gateway_kali.sh"
    echo "      (or edit /etc/ppp/peers/quota-wan + /etc/ppp/chap-secrets)."
    echo "   3) A static-IP device now has NO second router to bypass to: its only"
    echo "      gateway is $CLIENT_IP, so every byte crosses the box (quota +"
    echo "      blocks apply to it like any DHCP device). Keep IPv6/RA off on the"
    echo "      router/AP (Quota Manager is IPv4-only)."
    echo "   4) The dashboard WAN tab shows ppp0 state and toggles back to 'lan'"
    echo "      (applies on the next restart). Reverting fully = router back to"
    echo "      routed mode + re-run this script WITHOUT QUOTA_TOPOLOGY=wan."
    echo "   5) Start the gateway + watch it come up:"
    echo "        systemctl start quota-gateway"
    echo "        journalctl -u quota-gateway -f"
    echo "      Dashboard:  http://$CLIENT_IP:8080  (default password 'admin',"
    echo "      change it in Settings)."
else
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
    echo "   3) ARP gateway-lock (default ON, engine.gateway_arp_lock): a device that"
    echo "      sets a static IP + the ROUTER as its gateway bypasses the box — never"
    echo "      counted, never blocked, invisible. The lock captures the router IP on"
    echo "      the client subnet and drops the bypasser's internet. Static-IP devices"
    echo "      that SHOULD have internet must use the quota gateway ($CLIENT_IP)."
    echo "      To disable: set engine.gateway_arp_lock: false in $CONF_DIR/config.yaml."
    echo "      For routers that cannot remove the bypass at the source: enable MAC"
    echo "      filtering / client isolation on the router so unknown MACs are cut at"
    echo "      the router's own edge."
    echo "   4) Start the gateway:"
    echo "        systemctl start quota-gateway"
    echo "   5) Watch it come up:  journalctl -u quota-gateway -f"
    echo "      Dashboard:  http://$CLIENT_IP:8080  (default password 'admin',"
    echo "      change it in Settings)."
    echo
    echo "  Re-run this script anytime to re-apply settings (it stops first if the"
    echo "  app is running)."
fi
