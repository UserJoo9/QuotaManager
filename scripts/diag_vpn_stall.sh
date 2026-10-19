#!/usr/bin/env bash
# Run as root ON THE GATEWAY BOX while the VPN share is ON:
#   sudo bash diag_vpn_stall.sh            (default 90 s)
# Start it, wait ~10 s, then start the big download on one device, keep it
# running, and try to open a site on another device. Send me /tmp/vpn_diag.txt
OUT=/tmp/vpn_diag.txt; DUR=${1:-90}
: > "$OUT"; exec > >(tee -a "$OUT") 2>&1
NIC=$(ip -o link show type ppp 2>/dev/null | awk -F": " '{print $2}' | head -1); [ -z "$NIC" ] && NIC=$(ip -o route show default | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
TUN=quota-vpn
echo "== nic=$NIC tun=$TUN kernel=$(uname -r) $(date)"
echo "== ip rule"; ip rule
echo "== table 200"; ip route show table 200
echo "== main routes"; ip route
echo "== sysctl"; sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc \
  net.netfilter.nf_conntrack_max net.ipv4.ip_forward 2>&1
echo "== link mtu/qlen"; ip -d link show "$NIC"; [ -n "$TUN" ] && ip -d link show "$TUN"
echo "== tc BEFORE"; for d in "$NIC" eth0 ifb0 ifb1 $TUN; do echo "--- $d"; tc -s qdisc show dev $d 2>&1; done
echo "== sampling every 3s for ${DUR}s: time | ping-ppp0-direct (queue probe) | ping-router | conntrack | nic rx/tx KB/s | tun rx/tx KB/s | dropped(nic qdisc)"
RT=192.168.1.1
SRV=$(ss -tn state established 2>/dev/null | awk '/ppp0/ && /:443/{split($5,a,":"); print a[1]; exit}')
echo "vpn server (outer) = ${SRV:-unknown}"
prev_rx=0; prev_tx=0; prev_trx=0; prev_ttx=0
for ((i=0;i<DUR;i+=3)); do
  p1=$(ping -n -c1 -W5 -I "$NIC" 8.8.8.8 2>/dev/null | sed -n 's/.*time=\([0-9.]*\).*/\1/p'); [ -z "$p1" ] && p1=LOST
  p2=$(ping -n -c1 -W2 "$RT" 2>/dev/null | sed -n 's/.*time=\([0-9.]*\).*/\1/p'); [ -z "$p2" ] && p2=LOST
  oc=NA; [ -n "$SRV" ] && oc=$(curl -sk -o /dev/null --connect-timeout 8 -w "%{time_connect}" "https://$SRV/" 2>/dev/null || echo FAIL)
  ct=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null)
  rx=$(cat /sys/class/net/$NIC/statistics/rx_bytes); tx=$(cat /sys/class/net/$NIC/statistics/tx_bytes)
  if [ -n "$TUN" ]; then trx=$(cat /sys/class/net/$TUN/statistics/rx_bytes); ttx=$(cat /sys/class/net/$TUN/statistics/tx_bytes); else trx=0; ttx=0; fi
  dr=$(tc -s qdisc show dev $NIC 2>/dev/null | awk '/dropped/{gsub(",","",$7); s+=$7} END{print s+0}')
  echo "$(date +%T) | box:${p1}ms | router:${p2}ms | outer-connect=${oc}s | ct=$ct | nic $(( (rx-prev_rx)/3072 ))/$(( (tx-prev_tx)/3072 )) | tun $(( (trx-prev_trx)/3072 ))/$(( (ttx-prev_ttx)/3072 )) | qdrop=$dr"
  prev_rx=$rx; prev_tx=$tx; prev_trx=$trx; prev_ttx=$ttx
  sleep 3
done
echo "== tc AFTER"; for d in "$NIC" eth0 ifb0 ifb1; do echo "--- $d"; tc -s class show dev $d 2>&1 | head -80; tc -s qdisc show dev $d 2>&1; done
echo "== tcp retrans / listen overflow"; nstat -az TcpRetransSegs TcpExtListenOverflows TcpExtTCPTimeouts 2>&1
echo "== conntrack drops"; conntrack -S 2>&1 | head -5; dmesg | grep -i -E "conntrack|table full|drop" | tail -5
echo "== sing-box sockets (top)"; ss -tin state established 2>/dev/null | head -40
echo "== sing-box log tail"; journalctl -u quota-gateway -n 60 --no-pager 2>&1 | grep -i -E "sing|vpn|error|warn|timeout|reset" | tail -30
echo "== quota-gateway shaping log"; journalctl -u quota-gateway -n 400 --no-pager 2>&1 | grep -i -E "shap|tc |ifb|cake|vpn" | tail -30
echo DONE; echo "send: $OUT"
