#!/usr/bin/env bash
# Catch the "works ~3 min, then EVERYTHING stops, then recovers" stall.
# Run as root on the gateway, then reproduce (download + TikTok live):
#     sudo bash scripts/stall_watch.sh 900        # seconds to watch (default 600)
# Output: /tmp/stall_watch.txt  (send it back). Read-only: it changes nothing.
DUR=${1:-600}
OUT=/tmp/stall_watch.txt
WAN=$(ip -o route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
[ -z "$WAN" ] && WAN=ppp0
TUN=quota-vpn
rx(){ cat /sys/class/net/$1/statistics/rx_bytes 2>/dev/null || echo 0; }
tx(){ cat /sys/class/net/$1/statistics/tx_bytes 2>/dev/null || echo 0; }
snap(){
  echo "----- SNAPSHOT $1 $(date +%T) -----"
  echo "# outer sockets (local addr of $WAN)"
  LA=$(ip -4 -o addr show dev $WAN 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
  ss -tin src "$LA" 2>/dev/null | head -60
  echo "# tc ppp0 / ifb1"; tc -s qdisc show dev $WAN | head -30; tc -s qdisc show dev ifb1 | head -30
  echo "# ip -s link $WAN"; ip -s link show $WAN
  echo "# ip rule"; ip rule
  echo "# table 200 / main"; ip route show table 200; ip route show
  echo "# conntrack"; cat /proc/sys/net/netfilter/nf_conntrack_count
  echo "# journal (last 40)"; journalctl --since "-90 sec" --no-pager 2>/dev/null | grep -i -E "vpn|sing|ppp|quota|link|carrier" | tail -40
}
{
echo "== stall_watch start $(date) wan=$WAN tun=$TUN dur=${DUR}s (1 sample/s; the via-tunnel curl probe was removed: it blocked the loop and always failed)"
IDX0=$(ip -o link show $WAN | cut -d: -f1)
pr=$(rx $WAN); pt=$(tx $WAN); vr=$(rx $TUN); vt=$(tx $TUN)
state=ok; low=0; peak=0
for ((s=0; s<DUR; s++)); do
  sleep 1
  r=$(rx $WAN); t=$(tx $WAN); a=$(rx $TUN); b=$(tx $TUN)
  dr=$(( (r-pr)/1024 )); dt=$(( (t-pt)/1024 )); da=$(( (a-vr)/1024 )); db=$(( (b-vt)/1024 ))
  pr=$r; pt=$t; vr=$a; vt=$b
  IDX=$(ip -o link show $WAN 2>/dev/null | cut -d: -f1)
  [ "$IDX" != "$IDX0" ] && { echo "!! $WAN ifindex changed $IDX0 -> $IDX (REDIAL) $(date +%T)"; IDX0=$IDX; }
  line="$(date +%T) wan rx/tx ${dr}/${dt}KB/s tun rx/tx ${da}/${db}KB/s"
  echo "$line"
  (( dr > peak )) && peak=$dr
  if (( peak > 50 )); then
    if (( dr < 3 )); then low=$((low+1)); else low=0; fi
    if [ $state = ok ] && (( low >= 3 )); then state=stall; echo "!!!! STALL START $(date +%T)"; snap START; fi
    if [ $state = stall ] && (( dr > 30 )); then state=ok; echo "!!!! STALL END $(date +%T)"; snap END; peak=0; fi
  fi
done
echo "== done $(date)"
} > $OUT 2>&1
echo "wrote $OUT"
