#!/bin/bash
# Packaging smoke test - runs INSIDE a throwaway distro container (see
# .github/workflows/packaging-smoke.yml). Usage: smoke.sh <deb|rpm|arch> [dist-dir]
# Installs the real built package with the distro's package manager (so wrong
# dependency names fail here, not on a user's box), then checks payload, venv,
# imports, upgrade and removal. No systemd/NIC in a container, so the network
# setup step is expected to report its non-fatal warning.
set -eu
KIND="$1"; DIST="${2:-/dist}"

ok()   { echo "OK   $*"; }
fail() { echo "FAIL $*" >&2; exit 1; }

install_pkg() {
    case "$KIND" in
        deb)  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
        rpm)  dnf install -y -q "$@" ;;
        arch) pacman -Syu --noconfirm >/dev/null && pacman -U --noconfirm "$@" ;;
    esac
}

case "$KIND" in
    deb)  PKG="$(ls "$DIST"/*.deb)" ;;
    rpm)  PKG="$(ls "$DIST"/*.rpm)" ;;
    arch) PKG="$(ls "$DIST"/*.pkg.tar.zst)" ;;
    *) fail "unknown kind $KIND" ;;
esac

echo "== install $PKG"
install_pkg "$PKG" || fail "package install failed"

[ "$(cat /opt/quota-manager/.install-kind)" = "$KIND" ] || fail ".install-kind is not $KIND"
ok "install-kind marker = $KIND"
[ -x /opt/quota-manager/.venv/bin/python ] || fail "venv missing"
ok "venv created"
/opt/quota-manager/.venv/bin/python -c "import fastapi, uvicorn, yaml, aiosqlite" || fail "runtime deps do not import"
ok "runtime deps import"
(cd /opt/quota-manager && .venv/bin/python -c "import api.app, quota.service, quota.updater") || fail "app does not import"
ok "app modules import"
for f in /opt/quota-manager/scripts/*.sh; do bash -n "$f" || fail "syntax: $f"; done
ok "all scripts parse"

echo "== distro helper resolves real package names"
# shellcheck disable=SC1091
. /opt/quota-manager/scripts/distro.sh
PM="$(qm_detect_pm)"
# shellcheck disable=SC2046
qm_install_pkgs "$PM" $(qm_net_pkgs "$PM") $(qm_python_pkgs "$PM") || fail "package names not found for $PM"
ok "dependency names exist on $PM"

echo "== reinstall (upgrade path keeps the venv)"
touch /opt/quota-manager/.venv/KEEP
case "$KIND" in
    deb)  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --reinstall "$PKG" ;;
    rpm)  dnf reinstall -y -q "$PKG" ;;
    arch) pacman -U --noconfirm "$PKG" ;;
esac || fail "reinstall failed"
[ -e /opt/quota-manager/.venv/KEEP ] || fail "upgrade wiped the venv"
ok "upgrade keeps the venv"

echo "== remove"
case "$KIND" in
    deb)  DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq quota-manager ;;
    rpm)  dnf remove -y -q quota-manager ;;
    arch) pacman -R --noconfirm quota-manager ;;
esac || fail "remove failed"
[ ! -e /opt/quota-manager/run.py ] || fail "files left after remove"
ok "removed cleanly"
echo "SMOKE PASSED ($KIND)"
