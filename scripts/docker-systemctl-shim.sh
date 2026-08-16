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
                    kill -9 $(pidof dnsmasq) 2>/dev/null || true
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
                    kill -9 $(pidof dnsmasq) 2>/dev/null || true
                fi
                exit 0
                ;;
            quota-gateway)
                pkill -f "python.*run.py" || true
                exit 0
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
