#!/usr/bin/env bash
# Distro helpers shared by scripts/setup_gateway_kali.sh and install.sh.
# Source it (do not execute). Pure functions: nothing here touches the system
# except qm_install_pkgs.
#
# Supported package managers: apt (Debian/Ubuntu/Kali), dnf (Fedora/RHEL family),
# pacman (Arch/Manjaro), apk (Alpine, best effort).

# QM_PM can be forced from the environment (tests, odd derivatives).
qm_detect_pm() {
    if [ -n "${QM_PM:-}" ]; then echo "$QM_PM"; return 0; fi
    if command -v apt-get >/dev/null 2>&1; then echo apt
    elif command -v dnf >/dev/null 2>&1; then echo dnf
    elif command -v yum >/dev/null 2>&1; then echo dnf
    elif command -v pacman >/dev/null 2>&1; then echo pacman
    elif command -v apk >/dev/null 2>&1; then echo apk
    else echo unknown
    fi
}

# Packages the gateway needs on the host (network stack, no Python).
qm_net_pkgs() {
    case "${1:-$(qm_detect_pm)}" in
        apt)    echo "dnsmasq nftables iproute2 kmod ppp ca-certificates" ;;
        dnf)    echo "dnsmasq nftables iproute kmod ppp ca-certificates" ;;
        pacman) echo "dnsmasq nftables iproute2 kmod ppp ca-certificates" ;;
        apk)    echo "dnsmasq nftables iproute2 kmod ppp ca-certificates" ;;
        *)      echo "" ;;
    esac
}

# Python runtime packages (the app runs in a venv, so only the interpreter +
# venv/pip support is needed from the distro).
qm_python_pkgs() {
    case "${1:-$(qm_detect_pm)}" in
        apt)    echo "python3 python3-venv python3-pip" ;;
        dnf)    echo "python3 python3-pip" ;;
        pacman) echo "python python-pip" ;;
        apk)    echo "python3 py3-pip" ;;
        *)      echo "" ;;
    esac
}

# qm_install_pkgs <pm> <pkg>...   install non-interactively; returns the
# package manager's status. Never prompts.
qm_install_pkgs() {
    local pm="$1"; shift
    case "$pm" in
        apt)
            DEBIAN_FRONTEND=noninteractive apt-get update -qq
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
        dnf)    dnf install -y -q "$@" ;;
        pacman) pacman -Sy --needed --noconfirm "$@" ;;
        apk)    apk add --no-cache "$@" ;;
        *)      return 1 ;;
    esac
}

# Path of the pppd binary ("" when ppp is missing). Debian puts it in
# /usr/sbin, Arch in /usr/bin.
qm_pppd_bin() {
    command -v pppd 2>/dev/null && return 0
    for p in /usr/sbin/pppd /usr/bin/pppd; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}
