#!/usr/bin/env bash
# ===========================================================================
#  Quota Manager — turn the whole gateway off (System > Power in the dashboard)
# ---------------------------------------------------------------------------
#  Stops AND disables everything the gateway runs, so it stays off after a
#  reboot, and removes the kernel rules the app leaves in place on a normal
#  stop (nftables tables incl. the ARP gateway-lock, tc shaping). Started by
#  the app through `systemd-run`, a few seconds after the API answers, so it
#  outlives quota-gateway.service.
#
#  Turn it back on:   sudo systemctl enable --now dnsmasq quota-gateway
#  (WAN mode also:    sudo systemctl enable --now quota-wan-ppp)
#
#  Usage: gateway_off.sh [poweroff]
# ===========================================================================
set -u

log() { echo "[gateway-off] $*"; logger -t quota-gateway-off "$*" 2>/dev/null || true; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

log "turning the gateway off"
for unit in quota-wan-ppp dnsmasq quota-gateway; do
    systemctl disable --now "$unit" >/dev/null 2>&1 && log "stopped + disabled $unit"
done

for table in quota_gateway quota_firewall quota_nat quota_schedule; do
    nft delete table inet "$table" 2>/dev/null && log "removed nftables table $table"
done

for dev in /sys/class/net/*; do
    dev="${dev##*/}"
    [ "$dev" = lo ] && continue
    tc qdisc del dev "$dev" root 2>/dev/null || true
    tc qdisc del dev "$dev" ingress 2>/dev/null || true
done
log "speed shaping removed"

if [ "${1:-}" = poweroff ]; then
    log "powering the machine off"
    systemctl poweroff
fi
