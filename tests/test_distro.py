"""scripts/distro.sh: package-manager detection + per-distro package names."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts" / "distro.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _run(script: str, tmp_path: Path, tools=(), extra_env=None) -> str:
    """Run ``script`` after sourcing the lib with ONLY ``tools`` on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for t in tools:
        f = bindir / t
        f.write_text("#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    env = {"PATH": str(bindir)}
    env.update(extra_env or {})
    out = subprocess.run([BASH, "-c", f'. "{LIB}"; {script}'], env=env,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.parametrize("tool,pm", [("apt-get", "apt"), ("dnf", "dnf"),
                                     ("yum", "dnf"), ("pacman", "pacman"),
                                     ("apk", "apk")])
def test_detects_package_manager(tmp_path, tool, pm):
    assert _run("qm_detect_pm", tmp_path, [tool]) == pm


def test_unknown_and_forced(tmp_path):
    assert _run("qm_detect_pm", tmp_path) == "unknown"
    assert _run("qm_detect_pm", tmp_path, ["apt-get"], {"QM_PM": "pacman"}) == "pacman"


def test_debian_wins_when_several_exist(tmp_path):
    assert _run("qm_detect_pm", tmp_path, ["apt-get", "dnf", "pacman"]) == "apt"


def test_package_names_per_distro(tmp_path):
    net = {pm: _run(f"qm_net_pkgs {pm}", tmp_path).split() for pm in ("apt", "dnf", "pacman")}
    for pm, pkgs in net.items():
        assert {"dnsmasq", "nftables", "kmod", "ppp"} <= set(pkgs), pm
    assert "iproute2" in net["apt"] and "iproute2" in net["pacman"]
    assert "iproute" in net["dnf"] and "iproute2" not in net["dnf"]
    assert "python3-venv" in _run("qm_python_pkgs apt", tmp_path)
    assert _run("qm_python_pkgs pacman", tmp_path).split()[0] == "python"
    assert _run("qm_net_pkgs unknown", tmp_path) == ""


def test_pppd_bin_lookup(tmp_path):
    assert _run("qm_pppd_bin", tmp_path, ["pppd"]).endswith("/pppd")


def test_setup_script_uses_the_distro_layer():
    s = (ROOT / "scripts" / "setup_gateway_kali.sh").read_text()
    assert '. "$_DISTRO_LIB"' in s
    assert "qm_install_pkgs" in s and "qm_pppd_bin" in s
    assert "static_fallback wan" in s and "static_fallback lan" in s and "static_fallback ipv6" in s
    # the Debian install line stays byte-identical
    assert "apt-get install -y -qq dnsmasq nftables iproute2" in s
    # no unconditional ifupdown heredoc left outside the helper
    outside = s.split("static_fallback() {")[0] + s.split("# --- 3. static IPs on the LAN NIC")[1]
    assert "cat > /etc/network/interfaces.d" not in outside
