#!/usr/bin/env bash
# Quota Manager - generic installer for any systemd-based Linux distro.
# Run as root from inside the unpacked release tarball:
#     sudo ./install.sh                # install / upgrade (installs distro deps)
#     sudo ./install.sh --no-deps      # skip the package-manager step
#     sudo ./install.sh --uninstall    # remove the app (keeps data + config)
#     sudo ./install.sh --upgrade      # same as a plain install (used by the updater)
set -euo pipefail

APP_DIR=/opt/quota-manager
SERVICE=quota-gateway
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NO_DEPS=0
UNINSTALL=0
for a in "$@"; do
    case "$a" in
        --no-deps)   NO_DEPS=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --upgrade)   ;;   # what the in-app updater passes; install is already upgrade-safe
        -h|--help)   sed -n 2,6p "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

log() { echo "[quota-manager] $*"; }
die() { echo "[quota-manager] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./install.sh)"
command -v systemctl >/dev/null 2>&1 || die "systemd is required (systemctl not found)"
[ -d /run/systemd/system ] || die "systemd is not the running init system"

if [ "$UNINSTALL" -eq 1 ]; then
    log "stopping + disabling $SERVICE"
    systemctl stop "$SERVICE" 2>/dev/null || true
    systemctl disable "$SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/$SERVICE.service"
    systemctl daemon-reload 2>/dev/null || true
    rm -rf "$APP_DIR"
    log "removed $APP_DIR. Config (/etc/quota-gateway) and data (/var/lib/quota-gateway) were kept."
    exit 0
fi

[ -f "$HERE/run.py" ] && [ -d "$HERE/quota" ] || die "run this from the unpacked quota-manager tarball"
# shellcheck source=/dev/null
. "$HERE/scripts/distro.sh"

if [ "$NO_DEPS" -eq 0 ]; then
    PM="$(qm_detect_pm)"
    [ "$PM" != "unknown" ] || die "no supported package manager found - install the dependencies yourself and re-run with --no-deps"
    # shellcheck disable=SC2046
    PKGS="$(qm_net_pkgs "$PM") $(qm_python_pkgs "$PM")"
    log "installing dependencies with $PM: $PKGS"
    # shellcheck disable=SC2086
    qm_install_pkgs "$PM" $PKGS || die "dependency install failed (re-run with --no-deps if you installed them manually)"
fi

# Upgrade: stop the live service, keep the venv (pip is incremental), replace the code.
systemctl stop "$SERVICE" 2>/dev/null || true
mkdir -p "$APP_DIR"
for item in run.py requirements-linux.txt LICENSE core quota api web scripts bin; do
    [ -e "$HERE/$item" ] || continue
    rm -rf "${APP_DIR:?}/$item"
    cp -a "$HERE/$item" "$APP_DIR/$item"
done
chmod 755 "$APP_DIR"/scripts/*.sh 2>/dev/null || true
[ -f "$APP_DIR/bin/sing-box" ] && chmod 755 "$APP_DIR/bin/sing-box" || true

exec bash "$APP_DIR/scripts/post_install.sh" tarball
