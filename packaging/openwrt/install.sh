#!/bin/sh
# QuotaManager installer for OpenWrt (apk). Run as root from the project dir.
# Untested on hardware - report problems with the printed output.
set -e
APP=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$APP/packaging/openwrt"
retry() { for i in 1 2 3 4 5 6; do "$@" && return 0; sleep 2; done; return 1; }

retry apk update
retry apk add python3 python3-pip python3-cryptography python3-yaml python3-sqlite3 \
    nftables kmod-nft-nat kmod-nft-offload ip-full tc-full kmod-ifb kmod-sched \
    kmod-sched-core ca-bundle block-mount
# dnsmasq-full is required (ipset/nftset, tags, DHCPv6); swap it in if needed
apk info -e dnsmasq-full >/dev/null 2>&1 || {
    apk del dnsmasq 2>/dev/null || true
    retry apk add dnsmasq-full
}
CBOR2_BUILD_C_EXTENSION=0 pip install --break-system-packages -r "$HERE/requirements-openwrt.txt"
pip install --break-system-packages --no-deps webauthn==2.5.2 || echo "webauthn skipped (passkeys off)"

install -m 755 "$HERE/systemctl" /usr/bin/systemctl
install -m 755 "$HERE/quota-manager.init" /etc/init.d/quota-manager
mkdir -p /overlay/quota-gateway
/etc/init.d/quota-manager enable
/etc/init.d/quota-manager restart
echo "Done. Dashboard: http://<AP-IP>:8080  (default password: admin)"
