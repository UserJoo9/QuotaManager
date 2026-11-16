#!/bin/sh
# Quota Manager - OpenWrt installer (BusyBox ash, POSIX sh only).
#   sh install_openwrt.sh
# OpenWrt 25.12+ uses apk; 22.03-24.10 use opkg. This detects which one the
# router has, installs the dependencies, fetches the code into /opt/QuotaManager,
# installs the Python deps and registers the procd service + a systemctl shim.
#
# On architectures without prebuilt wheels (mipsel/mips/arm 32-bit, e.g. MT7621)
# it installs the PURE-PYTHON set (requirements-openwrt.txt: pydantic v1, no
# Rust/C builds) and takes cryptography/PyYAML from the package manager.
set -eu

APP_DIR=/opt/QuotaManager
REPO=${QM_REPO:-https://github.com/UserJoo9/QuotaManager.git}
PKGS="python3 python3-pip python3-cryptography python3-yaml python3-sqlite3 \
nftables kmod-nft-core kmod-nft-nat ip-full tc-full kmod-ifb kmod-sched \
dnsmasq-full git git-http ca-bundle"

log() { echo "[quota-manager] $*"; }
die() { echo "[quota-manager] ERROR: $*" >&2; exit 1; }
retry() { n=0; while [ $n -lt 6 ]; do "$@" && return 0; n=$((n+1)); sleep 2; done; return 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"

# dnsmasq-full conflicts with the stock dnsmasq package
if command -v apk >/dev/null 2>&1; then
    PM=apk
    log "package manager: apk (OpenWrt 25.12+)"
    retry apk update
    apk del dnsmasq 2>/dev/null || true
    # shellcheck disable=SC2086
    retry apk add $PKGS || die "apk add failed (see output above)"
elif command -v opkg >/dev/null 2>&1; then
    PM=opkg
    log "package manager: opkg"
    retry opkg update
    opkg remove dnsmasq 2>/dev/null || true
    # shellcheck disable=SC2086
    retry opkg install $PKGS || die "opkg install failed (see output above)"
else
    die "neither apk nor opkg found - is this OpenWrt?"
fi

# Low-RAM routers (<=256 MB): pip + the app can be OOM-killed -> add a swap file
# on persistent storage when there is room.
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [ "${MEM_KB:-0}" -lt 300000 ] && [ ! -f /overlay/swapfile ]; then
    FREE_KB=$(df -k /overlay | awk 'NR==2 {print $4}')
    if [ "${FREE_KB:-0}" -gt 1500000 ]; then
        log "low RAM - creating a 512 MB swap file on /overlay"
        dd if=/dev/zero of=/overlay/swapfile bs=1M count=512 2>/dev/null
        chmod 600 /overlay/swapfile
        mkswap /overlay/swapfile >/dev/null && swapon /overlay/swapfile || true
    fi
fi

if [ -d "$APP_DIR/.git" ]; then
    log "updating $APP_DIR"
    git -C "$APP_DIR" pull --ff-only
else
    log "cloning into $APP_DIR"
    git clone --depth 1 "$REPO" "$APP_DIR"
fi

ARCH=$(uname -m)
case "$ARCH" in
    x86_64|aarch64) REQ_NOTE="prebuilt wheels available" ;;
    *) REQ_NOTE="no prebuilt wheels for $ARCH - using the pure-Python set" ;;
esac
log "arch $ARCH: $REQ_NOTE"
export CBOR2_BUILD_C_EXTENSION=0
PIP="python3 -m pip install --no-input --break-system-packages"
case "$ARCH" in
    x86_64|aarch64)
        # Router flash is small: skip pytest/httpx/pyflakes (dev only).
        grep -v -E '^(pytest|httpx|pyflakes|PyYAML)' "$APP_DIR/requirements-linux.txt" > /tmp/qm-req.txt
        $PIP -r /tmp/qm-req.txt || { log "full set failed - falling back to pure-Python set"; $PIP -r "$APP_DIR/requirements-openwrt.txt"; }
        rm -f /tmp/qm-req.txt ;;
    *)
        $PIP -r "$APP_DIR/requirements-openwrt.txt" ;;
esac
# passkeys (optional; needs cryptography from the package manager)
$PIP --no-deps webauthn==2.5.2 || log "webauthn skipped (passkeys disabled)"

# --- dnsmasq (OpenWrt generates its config from /etc/config/dhcp) ------------
# The app writes its DNS-filter/tag fragments into /etc/dnsmasq.d and reads
# leases from /var/lib/misc/dnsmasq.leases (config.yaml defaults). Stock OpenWrt
# loads neither, so point dnsmasq at them via uci.
mkdir -p /etc/dnsmasq.d /var/lib/misc
if uci -q get dhcp.@dnsmasq[0] >/dev/null 2>&1; then
    uci -q set dhcp.@dnsmasq[0].confdir='/etc/dnsmasq.d'
    uci -q set dhcp.@dnsmasq[0].leasefile='/var/lib/misc/dnsmasq.leases'
    uci -q set dhcp.@dnsmasq[0].logdhcp='1'
    uci commit dhcp
    log "dnsmasq: confdir=/etc/dnsmasq.d, leasefile=/var/lib/misc/dnsmasq.leases"
fi
cat > /etc/dnsmasq.d/quota-dnslog.conf <<'DNSEOF'
# Written by install_openwrt.sh - browsing-history log read by QuotaManager
log-queries=extra
log-facility=/var/log/quota-dnsmasq.log
DNSEOF
# ifb (speed limits) loads at boot via kmod autoload; make sure it is up now
modprobe ifb 2>/dev/null || true
/etc/init.d/dnsmasq restart 2>/dev/null || true

install -m 755 "$APP_DIR/scripts/systemctl.openwrt" /usr/bin/systemctl
install -m 755 "$APP_DIR/scripts/quota-manager.openwrt" /etc/init.d/quota-manager
mkdir -p /overlay/quota-gateway
/etc/init.d/quota-manager enable
/etc/init.d/quota-manager restart || /etc/init.d/quota-manager start
log "done. Dashboard: http://<router-ip>:8080  (check: python3 $APP_DIR/run.py --doctor)"
