#!/usr/bin/env bash
# ==============================================================================
# systemctl shim for containerized QuotaManager
# Allows quota/dns_rules.py and scripts to issue systemctl restart commands
# for supported container services, and returns explicit failure for unsupported
# services (e.g. host-level ppp/wan networking).
# ==============================================================================

ACTION="$1"
SERVICE="$2"

case "$ACTION" in
    restart)
        case "$SERVICE" in
            dnsmasq)
                if pidof dnsmasq >/dev/null 2>&1; then
                    kill $(pidof dnsmasq) 2>/dev/null || true
                    sleep 0.2
                fi
                if command -v dnsmasq >/dev/null 2>&1; then
                    dnsmasq --conf-dir=/etc/dnsmasq.d,*.conf 2>/dev/null || true
                fi
                exit 0
                ;;
            quota-gateway)
                echo "[systemctl shim] Restarting quota-gateway process..."
                pkill -f "python.*run.py" || exit 0
                exit 0
                ;;
            quota-wan-ppp)
                # PPPoE dialing is a HOST-level service (pppd owns the line;
                # the WAN tab's Restart button renews the public IP). A
                # container cannot fake it — fail loudly so the panel shows
                # an honest "restart failed" instead of silently doing
                # nothing.
                echo "[systemctl shim] ERROR: quota-wan-ppp is a host-level PPPoE service and cannot be managed from inside the container." >&2
                exit 1
                ;;
            *)
                echo "[systemctl shim] ERROR: Service '$SERVICE' is not supported in container environment (host service or not installed)." >&2
                exit 1
                ;;
        esac
        ;;
    start)
        case "$SERVICE" in
            dnsmasq)
                if ! pidof dnsmasq >/dev/null 2>&1 && command -v dnsmasq >/dev/null 2>&1; then
                    dnsmasq --conf-dir=/etc/dnsmasq.d,*.conf 2>/dev/null || true
                fi
                exit 0
                ;;
            quota-wan-ppp)
                echo "[systemctl shim] ERROR: quota-wan-ppp is a host-level PPPoE service and cannot be started from inside the container." >&2
                exit 1
                ;;
            *)
                echo "[systemctl shim] ERROR: Service '$SERVICE' is not supported in container environment." >&2
                exit 1
                ;;
        esac
        ;;
    stop)
        case "$SERVICE" in
            dnsmasq)
                if pidof dnsmasq >/dev/null 2>&1; then
                    kill $(pidof dnsmasq) 2>/dev/null || true
                fi
                exit 0
                ;;
            quota-gateway)
                pkill -f "python.*run.py" || true
                exit 0
                ;;
            quota-wan-ppp)
                echo "[systemctl shim] ERROR: quota-wan-ppp is a host-level PPPoE service and cannot be stopped from inside the container." >&2
                exit 1
                ;;
            *)
                echo "[systemctl shim] ERROR: Service '$SERVICE' is not supported in container environment." >&2
                exit 1
                ;;
        esac
        ;;
    status)
        case "$SERVICE" in
            dnsmasq)
                if pidof dnsmasq >/dev/null 2>&1; then
                    exit 0
                else
                    exit 3
                fi
                ;;
            quota-gateway)
                if pgrep -f "python.*run.py" >/dev/null 2>&1; then
                    exit 0
                else
                    exit 3
                fi
                ;;
            quota-wan-ppp)
                # Unknown/inert by design: the container has no ppp0. Exit 4
                # ("unit not found") so a status probe reads as absent rather
                # than a lying "inactive".
                echo "[systemctl shim] quota-wan-ppp is a host-level PPPoE service — not present in the container (exit 4)." >&2
                exit 4
                ;;
            *)
                echo "[systemctl shim] Unit '$SERVICE' not found." >&2
                exit 4
                ;;
        esac
        ;;
    daemon-reload|enable|disable)
        exit 0
        ;;
    *)
        echo "[systemctl shim] Unknown action '$ACTION'" >&2
        exit 1
        ;;
esac
