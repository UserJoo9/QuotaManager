#!/usr/bin/env bash
# ===========================================================================
#  Quota Manager — nftables startup replay (diagnostic)
# ---------------------------------------------------------------------------
#  A box where the journal reads "nftables engine unavailable: nft add
#  failed: Error: No symbol type information" tells you a command failed, but
#  not WHICH one (the engine's _fail() log names no argv). This replays the
#  engine's EXACT startup commands one-by-one on a clean slate and stops at
#  the first failure with the command printed, so the culprit is named in a
#  single run.
#
#  SAFETY
#  * Deletes ONLY the two app-owned tables (inet quota_gateway + arp
#    quota_arp_lock) for a clean slate, then rebuilds them exactly as the
#    engine does. No other kernel state is touched.
#  * Does NOT restart the app. After the replay: `systemctl restart
#    quota-gateway` to re-arm the real engine.
#  * Ends with gw_blocked flushed (empty = box internet free), matching the
#    state the engine's start() leaves behind — the toggle test just proves
#    the command parses, it must not leave the box cut off.
#
#  The values below mirror a typical box's derived config:
#    router 192.168.1.1, client subnet 192.168.2.0/24,
#    local nets 192.168.1.0/24 + 192.168.2.0/24, count_gateway=on.
#  If your box differs, edit CLIENT/ROUTER/LOCAL1/LOCAL2 before running.
# ===========================================================================

set -u

T="inet quota_gateway"        # the app's inet table (engine.table)
A="arp quota_arp_lock"        # the ARP gateway-lock table
CLIENT="192.168.2.0/24"       # client subnet (engine.client_subnet)
ROUTER="192.168.1.1"          # the upstream router IP (engine.router_ip)
LOCAL1="192.168.1.0/24"       # uplink LAN (engine.uplink_subnet)
LOCAL2="192.168.2.0/24"       # client LAN (engine.client_subnet)

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

# Print + run an nft command; abort on failure with the exact argv named.
step() {
    printf '\n\e[1;36m>>> nft %s\e[0m\n' "$*"
    nft "$@" || { printf '\n\e[1;31m!!! FAILED at: nft %s\e[0m\n' "$*"; exit 1; }
}
# Best-effort cleanup command (e.g. deleting a table that may not exist).
best() {
    printf '\n(cleanup, errors ignored) nft %s\n' "$*"
    nft "$@" 2>/dev/null || true
}

# --- clean slate -------------------------------------------------------------
best delete table $T
best delete table $A

# --- start(): base table + forward chain + blocked set -----------------------
step add table $T
step flush table $T
step add chain $T forward "{ type filter hook forward priority 0; policy accept; }"
step add set $T blocked "{ type ipv4_addr; }"

# --- _program_gateway_lock() (ARP gateway-lock) ------------------------------
step add set $T known_ips "{ type ipv4_addr; }"
step add rule $T forward "ip saddr $CLIENT ip saddr != @known_ips drop"
step add table $A
step flush table $A
step add chain $A input "{ type filter hook input priority 0; policy accept; }"
step add rule $A input "arp operation 2 arp saddr ip $ROUTER arp daddr ip $CLIENT drop"

# --- forward blocked drops (reference @blocked) ------------------------------
step add rule $T forward "ip saddr @blocked ip daddr != $LOCAL1 ip daddr != $LOCAL2 drop"
step add rule $T forward "ip daddr @blocked ip saddr != $LOCAL1 ip saddr != $LOCAL2 drop"

# --- _program_gateway(): input/output hooks + gw_blocked interval set --------
step add chain $T input  "{ type filter hook input priority 0; policy accept; }"
step add chain $T output "{ type filter hook output priority 0; policy accept; }"
step add set $T gw_blocked "{ type ipv4_addr; flags interval; }"

# count_gateway=on: named counters + counting rules
step add counter $T q_gw_up
step add counter $T q_gw_down
step add rule $T output "ip daddr != $LOCAL1 ip daddr != $LOCAL2 counter name q_gw_up"
step add rule $T input  "ip saddr != $LOCAL1 ip saddr != $LOCAL2 counter name q_gw_down"

# DNS + DHCP exemptions BEFORE the gw_blocked drops
step add rule $T output "udp dport 53 accept"
step add rule $T input  "udp sport 53 accept"
step add rule $T output "udp sport 67 accept"
step add rule $T input  "udp sport 68 udp dport 67 accept"

# gw_blocked drops (reference the interval set @gw_blocked)
step add rule $T output "ip daddr @gw_blocked ip daddr != $LOCAL1 ip daddr != $LOCAL2 drop"
step add rule $T input  "ip saddr @gw_blocked ip saddr != $LOCAL1 ip saddr != $LOCAL2 drop"

# --- the admin toggle (what set_gateway_blocked(True) runs) ------------------
step add element $T gw_blocked "{ 0.0.0.0/0 }"

# --- best-effort reset (engine start()) + leave the box's internet free ------
best reset counters $T
best flush set $T gw_blocked

printf '\n\e[1;32mAll commands succeeded — the failure is elsewhere.\e[0m\n'
