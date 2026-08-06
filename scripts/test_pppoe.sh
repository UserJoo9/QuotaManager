#!/usr/bin/env bash
# ===========================================================================
#  Quota Manager — PPPoE connection test (called by the dashboard WAN tab)
# ---------------------------------------------------------------------------
#  Dials the PPPoE line with the credentials typed into the WAN tab on a
#  THROWAWAY interface (ppp200) and reports whether an internet connection is
#  established — WITHOUT changing the running topology:
#
#    * no config.yaml write, no DB write (the app never persists anything)
#    * the test dial uses `unit 200` -> ppp200, never the real ppp0
#    * no defaultroute / replacedefaultroute / usepeerdns — the test link
#      cannot hijack the box's routing or DNS
#    * the only routes touched are throwaway /32s for the two ping targets,
#      removed again in the EXIT trap
#    * /etc/ppp/chap-secrets + pap-secrets are backed up, written for the test
#      user only, and restored in the EXIT trap; the temp peer file is removed
#
#  Env (fed by quota/netmgr.py): PPP_IF (NIC reaching the ONT/modem),
#  PPPOE_USER, PPPOE_PASSWORD.
#
#  Output: one key=value per line on stdout, parsed by the app:
#    RESULT=success|auth-failed|no-pppoe-server|link-down|error
#    LOCAL=<ip>      PEER=<ip>       INTERNET=yes|no    DETAIL=<text>
# ===========================================================================

set -u

log()  { echo -e "\e[1;36m[pppoe-test]\e[0m $*"; }
fail() { echo "RESULT=error"; echo "DETAIL=$*"; exit 1; }

# --- input (from the app) -----------------------------------------------------
PPP_IF="${PPP_IF:-}"
PPPOE_USER="${PPPOE_USER:-}"
PPPOE_PASSWORD="${PPPOE_PASSWORD:-}"

[ "$(id -u)" -eq 0 ] || fail "must run as root"
[ -n "$PPP_IF" ] || fail "PPP_IF not set (the app passes the NIC to dial on)"
if command -v pppd >/dev/null 2>&1 || [ -x /usr/sbin/pppd ]; then
    PPPD="$(command -v pppd 2>/dev/null || echo /usr/sbin/pppd)"
else
    fail "pppd not found — install the 'ppp' package first (WAN mode needs it)"
fi

PEER_FILE=/etc/ppp/peers/quota-wan-test
LOG_FILE=/tmp/quota-pppoe-test.log
CHAP_BAK=/etc/ppp/chap-secrets.qtest.bak
PAP_BAK=/etc/ppp/pap-secrets.qtest.bak
HAD_CHAP=0
HAD_PAP=0
TEST_PID=""
HAD_ROUTES=""

cleanup() {
    [ -n "$TEST_PID" ] && kill "$TEST_PID" 2>/dev/null
    [ -n "$TEST_PID" ] && wait "$TEST_PID" 2>/dev/null
    if [ -n "$HAD_ROUTES" ]; then
        ip route del 8.8.8.8/32 dev ppp200 2>/dev/null || true
        ip route del 1.1.1.1/32 dev ppp200 2>/dev/null || true
    fi
    ip link del ppp200 2>/dev/null || true
    if [ "$HAD_CHAP" = 1 ]; then
        mv -f "$CHAP_BAK" /etc/ppp/chap-secrets 2>/dev/null || true
    else
        rm -f /etc/ppp/chap-secrets
    fi
    if [ "$HAD_PAP" = 1 ]; then
        mv -f "$PAP_BAK" /etc/ppp/pap-secrets 2>/dev/null || true
    else
        rm -f /etc/ppp/pap-secrets
    fi
    rm -f "$PEER_FILE"
    rm -f "$LOG_FILE"
}
trap cleanup EXIT

# --- back up the real secrets so the test can never leak them ---------------
[ -f /etc/ppp/chap-secrets ] && cp -a /etc/ppp/chap-secrets "$CHAP_BAK" && HAD_CHAP=1
[ -f /etc/ppp/pap-secrets ]  && cp -a /etc/ppp/pap-secrets  "$PAP_BAK"  && HAD_PAP=1

# --- temp peer: throwaway unit 200, no default route, no DNS change ---------
# NOTE: `noauth` is REQUIRED here. pppd enables `auth` (require the PEER to
# authenticate) by default whenever a `user` + secrets file are present — the
# BRAS rejects that and refuses to authenticate to the client ("peer refused to
# authenticate: terminating link"). noauth only drops the peer-auth requirement;
# the client still authenticates itself via PAP/CHAP when the peer asks.
cat > "$PEER_FILE" <<EOF
plugin pppoe.so
$PPP_IF
unit 200
noipdefault
hide-password
mtu 1492
mru 1492
maxfail 0
noauth
debug
logfile $LOG_FILE
EOF
if [ -n "$PPPOE_USER" ]; then
    echo "user \"$PPPOE_USER\"" >> "$PEER_FILE"
    # chap-secrets / pap-secrets format:  client  server  secret  IPs
    printf '%s\t*\t%s\t*\n' "$PPPOE_USER" "$PPPOE_PASSWORD" > /etc/ppp/chap-secrets
    printf '%s\t*\t%s\t*\n' "$PPPOE_USER" "$PPPOE_PASSWORD" > /etc/ppp/pap-secrets
    chmod 600 /etc/ppp/chap-secrets /etc/ppp/pap-secrets
else
    log "no PPPOE_USER — dialing with 'noauth', which most ISP lines reject"
fi

# --- dial (background, nodetach so WE own the process) ----------------------
"$PPPD" call quota-wan-test nodetach >/dev/null 2>&1 &
TEST_PID=$!

# --- wait up to ~15 s for ppp200 to come up ----------------------------------
LOCAL=""
PEER=""
for _ in $(seq 1 30); do
    line="$(ip -o -4 addr show dev ppp200 2>/dev/null)" || true
    if [ -n "$line" ]; then
        LOCAL="$(echo "$line" | sed -n 's/.* inet \([0-9.]*\).*/\1/p')"
        PEER="$(echo "$line" | sed -n 's/.* peer \([0-9.]*\).*/\1/p')"
        break
    fi
    sleep 0.5
done

if [ -n "$LOCAL" ]; then
    # link is up — verify internet WITHOUT touching the default route: add
    # throwaway /32s for the ping targets only, on the test link.
    ip route add 8.8.8.8/32 dev ppp200 2>/dev/null && HAD_ROUTES="$HAD_ROUTES 8.8.8.8"
    ip route add 1.1.1.1/32 dev ppp200 2>/dev/null && HAD_ROUTES="$HAD_ROUTES 1.1.1.1"
    INTERNET=no
    if ping -c 1 -W 2 -I ppp200 8.8.8.8 >/dev/null 2>&1 ||
       ping -c 1 -W 2 -I ppp200 1.1.1.1 >/dev/null 2>&1; then
        INTERNET=yes
    fi
    echo "RESULT=success"
    echo "LOCAL=$LOCAL"
    echo "PEER=$PEER"
    echo "INTERNET=$INTERNET"
    echo "DETAIL=PPPoE link is up (L:$LOCAL <-> P:$PEER); internet reachable: $INTERNET"
    exit 0
fi

# --- the link never came up — diagnose from the pppd log ---------------------
LOG_TEXT="$(cat "$LOG_FILE" 2>/dev/null || true)"
# "no PPPoE server answered": the classic PADO-timeout message, OR the log shows
# discovery PADIs but never reached a session (some rp-pppoe builds log only the
# PADI retransmits, with no explicit timeout line).
if echo "$LOG_TEXT" | grep -qi "Timeout waiting for PADO" ||
   { echo "$LOG_TEXT" | grep -qi "PPPOE Discovery" && ! echo "$LOG_TEXT" | grep -qi "PPP session is"; }; then
    echo "RESULT=no-pppoe-server"
    echo "DETAIL=no PPPoE server answered on $PPP_IF — the box is sending PPPoE discovery but nothing replies. Check: (1) the router's DSL line is actually synced (no sync = no PPPoE), (2) the router is set to bridge/modem mode (a routed/NAT router does NOT pass PPPoE discovery), (3) right after an aborted session some ISPs ignore new discovery for ~a minute — wait and retry"
elif echo "$LOG_TEXT" | grep -qi "Authentication failed"; then
    echo "RESULT=auth-failed"
    echo "DETAIL=the ISP rejected the PPPoE user/password — double-check the credentials from your ISP"
elif echo "$LOG_TEXT" | grep -qi "peer refused to authenticate"; then
    echo "RESULT=link-down"
    echo "DETAIL=pppd required the peer to authenticate and the peer refused — the peer file is missing 'noauth'; re-sync the scripts (this build should already have it)"
elif echo "$LOG_TEXT" | grep -qi "LCP: timeout sending Config-Requests"; then
    echo "RESULT=link-down"
    echo "DETAIL=PPPoE server found but the PPP session stalled (LCP timeout) — this is usually an ISP-side or modem issue; try again"
else
    echo "RESULT=link-down"
    echo "DETAIL=PPPoE link could not be established on $PPP_IF"
fi
# Always surface the real pppd output — the reason is in the log tail.
if [ -s "$LOG_FILE" ]; then
    echo "--- pppd log (last 25 lines) ---"
    tail -n 25 "$LOG_FILE" | sed 's/^/  /'
fi
exit 0
