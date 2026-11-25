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
    deb)  DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq netrex ;;
    rpm)  dnf remove -y -q netrex ;;
    arch) pacman -R --noconfirm quota-manager ;;
esac || fail "remove failed"
[ ! -e /opt/quota-manager/run.py ] || fail "files left after remove"
ok "removed cleanly"

# ---------------------------------------------------------------------------
# Rename migration: a box that installed the OLD package name (the real, published
# quota-manager 0.5.11) must move to netrex without losing its files/venv, and the
# old package must be gone afterwards. This is the path every existing install takes.
# ---------------------------------------------------------------------------
OLD_VER=0.5.11
BASE="https://github.com/UserJoo9/Netrex/releases/download/v${OLD_VER}"
case "$KIND" in
    deb)  OLD_FILE="quota-manager_${OLD_VER}_all.deb" ;;
    rpm)  OLD_FILE="quota-manager-${OLD_VER}-1.noarch.rpm" ;;
    arch) OLD_FILE="quota-manager-${OLD_VER}-1-any.pkg.tar.zst" ;;
esac
echo "== migration: old quota-manager ${OLD_VER} -> netrex"
if ! command -v curl >/dev/null 2>&1; then
    case "$KIND" in
        deb) apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates ;;
        rpm) dnf install -y -q curl ;;
    esac
fi
mkdir -p /tmp/old
curl -fsSL -o "/tmp/old/$OLD_FILE" "$BASE/$OLD_FILE" || fail "could not download the old $OLD_FILE"
install_pkg "/tmp/old/$OLD_FILE" || fail "old package install failed"
[ -x /opt/quota-manager/.venv/bin/python ] || fail "old package left no venv"
touch /opt/quota-manager/.venv/KEEP-MIGRATION
mkdir -p /etc/quota-gateway && echo "# keep me" > /etc/quota-gateway/KEEP-CONFIG

case "$KIND" in
    deb)  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$PKG" ;;
    rpm)  dnf install -y -q "$PKG" ;;
    arch) pacman -U --noconfirm "$PKG" ;;
esac || fail "installing netrex over quota-manager failed"

case "$KIND" in
    deb)  dpkg -s netrex >/dev/null 2>&1 || fail "netrex not installed after migration"
          if dpkg -s quota-manager 2>/dev/null | grep -q '^Status: install ok installed'; then
              fail "old quota-manager still installed next to netrex"
          fi ;;
    rpm)  rpm -q netrex >/dev/null || fail "netrex not installed after migration"
          ! rpm -q quota-manager >/dev/null 2>&1 || fail "old quota-manager still installed" ;;
    arch) # same pacman package name on purpose (see packaging/arch/PKGBUILD), so
          # the installed package is simply upgraded in place
          pacman -Q quota-manager >/dev/null || fail "package missing after migration" ;;
esac
ok "old package replaced by netrex"
[ -e /opt/quota-manager/.venv/KEEP-MIGRATION ] || fail "migration wiped the venv"
[ -e /etc/quota-gateway/KEEP-CONFIG ] || fail "migration touched /etc/quota-gateway"
[ -f /opt/quota-manager/run.py ] || fail "app files missing after migration"
(cd /opt/quota-manager && .venv/bin/python -c "import api.app, quota.service") || fail "app does not import after migration"
ok "venv, config and app survive the migration"

if [ "$KIND" = deb ]; then
    echo "== migration through an apt repository (the documented upgrade command)"
    DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq netrex >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "/tmp/old/$OLD_FILE" >/dev/null || fail "old deb reinstall failed"
    touch /opt/quota-manager/.venv/KEEP-APT
    NEWVER="$(dpkg-deb -f "$PKG" Version)"
    # The real transitional package carries the release version (>= 0.6.0, the
    # version netrex Breaks below). Pin 0.6.0 so this also runs before the bump.
    DUMMYVER=0.6.0
    DUMMY=/tmp/dummy; rm -rf "$DUMMY"; mkdir -p "$DUMMY/DEBIAN"
    printf 'Package: quota-manager\nVersion: %s\nSection: oldlibs\nPriority: optional\nArchitecture: all\nDepends: netrex (>= %s)\nMaintainer: ci <ci@example.org>\nDescription: transitional\n x\n' \
        "$DUMMYVER" "$NEWVER" > "$DUMMY/DEBIAN/control"
    mkdir -p /tmp/repo && cp "$PKG" /tmp/repo/ && dpkg-deb --build --root-owner-group "$DUMMY" /tmp/repo/quota-manager_"$DUMMYVER"_all.deb >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq apt-utils >/dev/null
    (cd /tmp/repo && apt-ftparchive packages . > Packages)
    echo "deb [trusted=yes] file:/tmp/repo ./" > /etc/apt/sources.list.d/netrex-test.list
    apt-get update -qq >/dev/null 2>&1
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --only-upgrade quota-manager || fail "apt upgrade of quota-manager failed"
    dpkg -s netrex 2>/dev/null | grep -q '^Status: install ok installed' || fail "apt did not pull in netrex"
    [ -e /opt/quota-manager/.venv/KEEP-APT ] || fail "apt migration wiped the venv"
    [ -f /opt/quota-manager/run.py ] || fail "app files missing after apt migration"
    ok "apt upgrade quota-manager -> netrex"
fi
echo "SMOKE PASSED ($KIND)"
