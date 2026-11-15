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
# The dependencies come from PyPI, so this step needs internet. A failure used
# to end the script silently (the package manager still said "installed") and
# left a gateway that could not start. Retry, then say exactly what to do.
pip_ok=0
for attempt in 1 2 3; do
    if "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --no-input \
        --quiet --upgrade -r "$APP_DIR/requirements-linux.txt"; then
        pip_ok=1
        break
    fi
    log "could not download the Python dependencies (attempt $attempt of 3)"
    sleep 5
done
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
        if [ -n "$_lan" ] && [ -z "${LAN_IF:-}" ] && [ -e "/sys/class/net/$_lan" ] \
           && [ ! -e "/sys/devices/virtual/net/$_lan" ]; then
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
