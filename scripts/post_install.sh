#!/bin/bash
# Quota Manager post-install — shared by EVERY installer (deb postinst, rpm %post,
# Arch .INSTALL, the generic tarball's install.sh). Usage:
#     bash /opt/quota-manager/scripts/post_install.sh <deb|rpm|arch|tarball>
#
# The package payload only carries the application code. Everything stateful is
# created here:
#   1. a Python venv + the pinned dependencies (first install; reuses on upgrade)
#   2. the network stack + config + systemd unit, via scripts/setup_gateway_kali.sh
#      with QUOTA_NO_APT=1 (the installer already installed dnsmasq/nftables/...)
#   3. the gateway service is enabled and started
#
# The setup script preserves an existing /etc/quota-gateway/config.yaml (bundle,
# timezone) across installs — re-running it never clobbers a real bundle.
set -euo pipefail

APP_DIR=/opt/quota-manager
SERVICE=quota-gateway
CONF_DIR=/etc/quota-gateway

log() { echo "[quota-manager] $*"; }

# Which installer called us (deb | rpm | arch | tarball). The updater reads this
# marker to pick the right release asset for the next update.
if [ -n "${1:-}" ]; then
    echo "$1" > "$APP_DIR/.install-kind" 2>/dev/null || true
fi

log "installing Python runtime into $APP_DIR/.venv"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    python3 -m venv "$APP_DIR/.venv"
fi
# The dependencies come from PyPI, so this step needs internet.
#
# Some package managers run install scripts with NO network access (seen with
# pacman on CachyOS: pip fails with "Network is unreachable" inside the
# transaction while the very same command works right after it). So when the
# download fails here, hand the rest of the setup to systemd, which runs it a few
# seconds later outside the package manager. Only when that is not possible, or
# the deferred run fails too, tell the admin how to finish by hand.
SETUP_UNIT=quota-manager-setup
can_defer=0
if [ "${QUOTA_DEFERRED:-}" != 1 ] && command -v systemd-run >/dev/null 2>&1 \
        && [ -d /run/systemd/system ]; then
    can_defer=1
fi

# Quick silent probe: if inside a sandboxed package manager with no network,
# defer immediately to systemd instead of letting pip spew connection errors.
net_ok=1
if [ "$can_defer" = 1 ]; then
    if ! python3 -c "import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=2)" >/dev/null 2>&1; then
        net_ok=0
    fi
fi

# Download the dependencies (two tries). Sets pip_ok=1 on success.
pip_ok=0
try_pip() {
    for attempt in 1 2; do
        if "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --no-input \
            --quiet --retries 1 --upgrade -r "$APP_DIR/requirements-linux.txt"; then
            pip_ok=1
            return 0
        fi
        log "could not download the Python dependencies (attempt $attempt of 2)"
        sleep 3
    done
    return 1
}

if [ "$net_ok" = 1 ]; then
    try_pip || true
fi
if [ "$pip_ok" != 1 ]; then
    if [ "$can_defer" = 1 ]; then
        systemctl stop "$SETUP_UNIT.timer" "$SETUP_UNIT.service" >/dev/null 2>&1 || true
        systemctl reset-failed "$SETUP_UNIT.service" >/dev/null 2>&1 || true
        if systemd-run --quiet --collect --unit="$SETUP_UNIT" --on-active=5 \
                --setenv=QUOTA_DEFERRED=1 \
                /bin/bash "$APP_DIR/scripts/post_install.sh" "${1:-}" >/dev/null 2>&1; then
            log "package manager is sandboxed (no network) - setup will finish automatically"
            log "in the background in about a minute. Follow progress with: journalctl -fu $SETUP_UNIT"
            exit 0
        fi
    fi
    # The quick probe may have been wrong (slow link, a pip mirror instead of
    # pypi.org): when it skipped pip and deferring was impossible, try pip now
    # before telling the admin the setup failed.
    if [ "$net_ok" != 1 ]; then
        try_pip || true
    fi
fi
if [ "$pip_ok" != 1 ]; then
    echo "" >&2
    echo "[quota-manager] ERROR: SETUP IS NOT FINISHED." >&2
    echo "[quota-manager] The Python dependencies could not be downloaded from pypi.org" >&2
    echo "[quota-manager] (no internet, or pypi.org is unreachable from this machine)." >&2
    echo "[quota-manager] Quota Manager will NOT start until this is completed." >&2
    echo "[quota-manager] Connect this machine to the internet, then run:" >&2
    echo "[quota-manager]     sudo bash $APP_DIR/scripts/post_install.sh ${1:-}" >&2
    echo "" >&2
    exit 1
fi

if [ -f "$APP_DIR/bin/sing-box" ]; then
    chmod +x "$APP_DIR/bin/sing-box"
fi

# SELinux (Fedora/RHEL): give the freshly laid-down tree its default labels.
if command -v restorecon >/dev/null 2>&1; then
    restorecon -R "$APP_DIR" >/dev/null 2>&1 || true
fi

# Re-run the setup script so this install (re)applies sysctl, the client-subnet
# NAT, dnsmasq, the ifb module, the app config and the systemd unit. It is
# idempotent and refuses to run while the service is live. We stop it first so
# an upgrade can re-run setup cleanly.
systemctl stop "$SERVICE" 2>/dev/null || true

# --- keep WAN mode across upgrades ------------------------------------------
# The setup script regenerates config.yaml for the topology it is given
# (default: whatever config.yaml says) and drops the dashboard's topology
# override from the DB. A box switched to WAN from the dashboard therefore came
# back in LAN mode after every update. Work out the CURRENT topology (DB
# override first, then config.yaml) and, when it is WAN, hand the setup script
# the same PPPoE login/NIC it already holds, so the upgrade re-applies WAN.
DB=/var/lib/quota-gateway/quota.db
CUR_TOPO=""
if [ -f "$DB" ] && command -v python3 >/dev/null 2>&1; then
    CUR_TOPO="$(python3 - "$DB" <<'PYEOF' 2>/dev/null || true
import sqlite3, sys
try:
    db = sqlite3.connect(sys.argv[1])
    g = lambda k: (db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone() or [""])[0]
    if g("topology_source") == "dashboard" and g("topology") in ("lan", "wan"):
        print(g("topology"))
except Exception:
    pass
PYEOF
)"
fi
if [ -z "$CUR_TOPO" ] && [ -f "$CONF_DIR/config.yaml" ]; then
    CUR_TOPO="$(awk '/^[[:space:]]*topology:/{gsub(/["'"'"']/,"",$2); print $2; exit}' "$CONF_DIR/config.yaml")"
fi
if [ -z "${QUOTA_TOPOLOGY:-}" ] && [ "$CUR_TOPO" = "wan" ]; then
    log "WAN mode detected - keeping it across this update"
    export QUOTA_TOPOLOGY=wan
    PEER=/etc/ppp/peers/quota-wan
    if [ -f "$PEER" ]; then
        _u="$(sed -n 's/^user "\(.*\)"$/\1/p' "$PEER" | head -1)"
        _if="$(awk '/^plugin/{getline; print $1; exit}' "$PEER")"
        if [ -n "$_u" ] && [ -z "${PPPOE_USER:-}" ]; then
            export PPPOE_USER="$_u"
            _p="$(grep -F "\"$_u\" *" /etc/ppp/chap-secrets 2>/dev/null | head -1 | sed -n 's/^"[^"]*" \* "\(.*\)" \*$/\1/p' || true)"
            [ -n "$_p" ] && export PPPOE_PASSWORD="$_p"
        fi
        # The client-facing NIC is shaping.interface — written either as a flow
        # mapping (`shaping: {enabled: true, interface: lan0, ...}`) or as a
        # block. The old `^interface:` match hit `dhcp.interface: ''` first, so
        # _lan was the literal '' and the real NIC was never handed to the
        # setup script (which then auto-detected — and picked ifb0).
        _lan="$(awk '
            /^shaping:/ { s = 1
                if (match($0, /interface: *[A-Za-z0-9_.-]+/)) {
                    v = substr($0, RSTART, RLENGTH); sub(/interface: */, "", v); print v; exit }
                next }
            s && /^[^ ]/ { exit }
            s && match($0, /interface: *[A-Za-z0-9_.-]+/) {
                v = substr($0, RSTART, RLENGTH); sub(/interface: */, "", v); print v; exit }
        ' "$CONF_DIR/config.yaml" 2>/dev/null || true)"
        # Hand the box's own NIC back to the setup script - a bridge/bond/VLAN
        # is a valid choice the admin made. Never a shaping/PPP/tunnel device:
        # a config already broken by the ifb0 bug must fall through to the
        # (fixed) auto-detect instead of being locked onto ifb0.
        case "$_lan" in ifb*|ppp*|tun*|tap*|veth*|docker*|virbr*) _lan="" ;; esac
        if [ -n "$_lan" ] && [ -z "${LAN_IF:-}" ] && [ -e "/sys/class/net/$_lan" ]; then
            export LAN_IF="$_lan"
        fi
        if [ -n "$_if" ] && [ -n "$_lan" ] && [ "$_if" != "$_lan" ] && [ -z "${WAN_IF:-}" ]; then
            export WAN_IF="$_if"
        fi
    fi
fi

# A failing setup step must never leave the package half-configured with the
# service disabled (prerm disables it on upgrade): report it, but carry on and
# still enable + start the gateway below.
if ! APP_DIR="$APP_DIR" QUOTA_NO_APT=1 bash "$APP_DIR/scripts/setup_gateway_kali.sh"; then
    echo "[quota-manager] WARNING: setup script reported an error - continuing; re-run" \
         "'bash $APP_DIR/scripts/setup_gateway_kali.sh' and check the output" >&2
fi
systemctl daemon-reload 2>/dev/null || true

log "enabling + starting $SERVICE"
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
if systemctl start "$SERVICE" 2>/dev/null; then
    log "quota-gateway started"
else
    echo "[quota-manager] WARNING: could not start quota-gateway — check 'journalctl -u quota-gateway'" >&2
fi

log "done. Dashboard: http://$(awk '/^[[:space:]]*gateway_ip:/{print $2}' "$CONF_DIR/config.yaml" 2>/dev/null || echo 192.168.2.1):8080"

exit 0
