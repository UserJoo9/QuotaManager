#!/bin/sh
# Quota Manager - OpenWrt installer (BusyBox ash, POSIX sh only).
#   sh install_openwrt.sh
# OpenWrt 25.12+ replaced opkg with apk; 22.03-24.10 still use opkg. This
# detects which one the router has, installs the dependencies, fetches the code
# into /opt/QuotaManager, installs the Python deps (no dev/test packages) and
# registers the procd service.
set -eu

APP_DIR=/opt/QuotaManager
REPO=https://github.com/UserJoo9/QuotaManager.git
PKGS="python3 python3-pip nftables kmod-nft-core ip-full dnsmasq-full git git-http ca-bundle"

log() { echo "[quota-manager] $*"; }
die() { echo "[quota-manager] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"

if command -v apk >/dev/null 2>&1; then
    log "package manager: apk (OpenWrt 25.12+)"
    apk update
    # shellcheck disable=SC2086
    apk add $PKGS
elif command -v opkg >/dev/null 2>&1; then
    log "package manager: opkg"
    opkg update
    # shellcheck disable=SC2086
    opkg install $PKGS
else
    die "neither apk nor opkg found - is this OpenWrt?"
fi

if [ -d "$APP_DIR/.git" ]; then
    log "updating $APP_DIR"
    git -C "$APP_DIR" pull --ff-only
else
    log "cloning into $APP_DIR"
    git clone --depth 1 "$REPO" "$APP_DIR"
fi

# Router flash is small: skip pytest/httpx/pyflakes (dev only).
grep -v -E '^(pytest|httpx|pyflakes)' "$APP_DIR/requirements-linux.txt" > /tmp/qm-req.txt
if python3 -m venv "$APP_DIR/.venv" 2>/dev/null; then
    "$APP_DIR/.venv/bin/python" -m pip install --no-input -r /tmp/qm-req.txt
else
    log "python3 venv unavailable - installing into the system Python"
    python3 -m pip install --no-input -r /tmp/qm-req.txt
fi
rm -f /tmp/qm-req.txt

cp "$APP_DIR/scripts/quota-manager.openwrt" /etc/init.d/quota-manager
chmod +x /etc/init.d/quota-manager
/etc/init.d/quota-manager enable
/etc/init.d/quota-manager restart || /etc/init.d/quota-manager start
log "done. Dashboard: http://<router-ip>:8080"
