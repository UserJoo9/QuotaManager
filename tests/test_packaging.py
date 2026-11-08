"""Packaging contract tests.

The .deb is built and released ONLY by GitHub Actions
(``.github/workflows/release.yml``), which renders ``packaging/DEBIAN/control``
from ``quota/version.py``. This suite pins the static contract of those
artifacts so the pipeline is testable on any machine (no ``dpkg``/``dpkg-deb``
needed) and a bad packaging edit fails CI instead of producing a broken .deb.

Nothing here shells out — every check is file contents / metadata.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
PACKAGING = REPO / "packaging"
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
APT_WORKFLOW = REPO / ".github" / "workflows" / "apt-repo.yml"
PUBLIC_KEY = REPO / "quota-manager.gpg"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _git_index_mode(relpath: str) -> str:
    """Read the file mode from git's index.

    The working-tree stat on Windows never carries the execute bit, but the
    committed blob does (and that is what GitHub's checkout + dpkg-deb use).
    `git ls-files --stage` reports the mode git actually records.
    """
    out = subprocess.run(
        ["git", "ls-files", "--stage", "--", relpath],
        capture_output=True, text=True, check=True, cwd=REPO,
    ).stdout
    m = re.search(r"^(\d{6})\s+\w+\s+0\s+" + re.escape(relpath) + r"\s*$", out, re.M)
    assert m, f"{relpath} is not tracked — cannot check its mode"
    return m.group(1)


# --------------------------------------------------------------------------- #
# quota/version.py is the single source of truth
# --------------------------------------------------------------------------- #

def test_version_is_single_sourced():
    text = _read(REPO / "quota" / "version.py")
    m = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)[\'"]', text)
    assert m, "quota/version.py must define __version__ = 'x.y.z'"
    assert re.fullmatch(r"\d+\.\d+\.\d+", m.group(1)), f"version {m.group(1)!r} must be semver"


def test_version_is_reported_by_api_and_dashboard():
    """The release version must actually reach the dashboard."""
    from api.app import create_app
    from quota import db as _db
    from quota.engine import SnapshotHolder
    from quota.service import QuotaService
    from quota.version import __version__

    import asyncio

    class _DB(_db.Database):
        pass

    database = _DB(":memory:")
    service = QuotaService(database, timezone="Africa/Cairo")
    holder = SnapshotHolder()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(database.connect())
        app = create_app(database, service, holder)
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            c.post("/api/login", json={"password": "admin"})
            r = c.get("/api/dashboard")
            assert r.status_code == 200
            assert r.json()["version"] == __version__
            assert app.version == __version__
    finally:
        loop.run_until_complete(database.close())
        loop.close()


# --------------------------------------------------------------------------- #
# The GitHub Actions release workflow
# --------------------------------------------------------------------------- #

def test_release_workflow_exists_and_parses_as_yaml():
    assert WORKFLOW.is_file(), ".github/workflows/release.yml must exist"
    data = yaml.safe_load(_read(WORKFLOW))
    assert isinstance(data, dict) and "on" in data or True in data, \
        "the workflow must declare a trigger (YAML 1.1 parses 'on' as boolean True)"
    triggers = data.get("on", data.get(True, {}))
    assert isinstance(triggers, dict)
    tags = triggers.get("push", {}).get("tags", [])
    assert any(re.fullmatch(r"v\*", t) for t in tags), \
        "the workflow must trigger on push of a 'v*' tag"


def test_release_workflow_publishes_to_github_releases():
    text = _read(WORKFLOW)
    assert "softprops/action-gh-release" in text, "the .deb must go to GitHub Releases"
    assert "dist/quota-manager_*.deb" in text
    assert "permissions:" in text and "contents: write" in text


def test_release_workflow_verifies_tag_matches_version():
    text = _read(WORKFLOW)
    assert "quota/version.py" in text, "version must be read from quota/version.py"
    assert "GITHUB_REF" in text and "refs/tags/" in text, \
        "the tag must be checked against the version so a wrong tag fails loudly"


def test_release_workflow_ships_runtime_payload_only():
    # the payload allowlist lives in the shared build script (release.yml and
    # the packaging-smoke workflow both call it)
    assert "packaging/build_packages.sh" in _read(WORKFLOW)
    text = _read(REPO / "packaging" / "build_packages.sh")
    for needle in ("core quota api web scripts", "requirements-linux.txt"):
        assert needle in text
    assert ".git" not in text.replace(".github", ""), \
        ".git must not be staged into the package"


def test_release_workflow_embeds_changelog_in_release_body():
    """The release description must be auto-composed from CHANGELOG.md (the
    single source of truth for release notes), not a hand-edited body."""
    text = _read(WORKFLOW)
    assert "body_path" in text, \
        "the release body must be a generated file (softprops body_path)"
    assert "CHANGELOG.md" in text, \
        "the release notes must be extracted from CHANGELOG.md"


# --------------------------------------------------------------------------- #
# The apt-repository workflow (signed apt repo on GitHub Pages)
# --------------------------------------------------------------------------- #

def test_apt_repo_workflow_exists_and_parses_as_yaml():
    """apt-repo.yml publishes a signed apt repo to gh-pages. It must exist and
    declare BOTH triggers: a workflow_run on `release` (auto-publish every new
    tag) and a workflow_dispatch `version` input (manual backfill)."""
    assert APT_WORKFLOW.is_file(), ".github/workflows/apt-repo.yml must exist"
    data = yaml.safe_load(_read(APT_WORKFLOW))
    assert isinstance(data, dict), "apt-repo.yml must parse as a dict"
    triggers = data.get("on", data.get(True, {}))  # YAML 1.1 parses 'on' as True
    assert isinstance(triggers, dict)
    assert "workflow_run" in triggers, "apt-repo must trigger on workflow_run"
    assert "workflow_dispatch" in triggers, "apt-repo must be manually dispatchable"


def test_apt_repo_workflow_triggers_on_release_completion():
    text = _read(APT_WORKFLOW)
    assert "workflow_run" in text
    assert "types: [completed]" in text or "completed" in text
    assert "workflow_run.conclusion" in text, \
        "the job must gate on the release run's conclusion"
    assert "success" in text, "the workflow must only publish on a successful release"
    assert '"release"' in text or "release" in text, \
        "apt-repo must be chained to the release workflow"


def test_apt_repo_workflow_has_manual_backfill_input():
    text = _read(APT_WORKFLOW)
    assert "workflow_dispatch" in text
    assert "version" in text and "inputs:" in text, \
        "the workflow_dispatch must accept a version input"
    assert "No version supplied" in text, \
        "an empty backfill version must fail loudly with a usage message"


def test_apt_repo_workflow_downloads_the_deb_from_releases():
    text = _read(APT_WORKFLOW)
    assert "gh release download" in text, \
        "the .deb must come from the GitHub Release asset (not a local build)"
    assert "quota-manager_" in text and "_all.deb" in text, \
        "the download must target quota-manager_<ver>_all.deb"
    assert "GH_TOKEN" in text or "GITHUB_TOKEN" in text, \
        "gh must be authenticated with the Actions token"


def test_apt_repo_workflow_signs_and_publishes():
    text = _read(APT_WORKFLOW)
    for needle in (
        "APT_REPO_GPG_KEY",      # the armored private signing key secret
        "apt-ftparchive",        # Packages / Release index generator
        "apt-utils",             # ships apt-ftparchive
        "gnupg",
        "sudo apt-get",          # the runner is not root — without sudo the
                                 #   tool install died with E: lock permission
        "--detach-sign",         # Release.gpg
        "--clear-sign",          # InRelease
        "Release.gpg",
        "InRelease",
        ".nojekyll",             # REQUIRED or Jekyll strips pool/ + dists/
        "pool/",
        "dists/stable/main/binary-all",
        "gh-pages",
        "quota-manager.gpg",     # the committed public key copied into the repo
    ):
        assert needle in text, f"apt-repo.yml must reference {needle!r}"


def test_apt_repo_workflow_fpr_extraction_is_quote_safe():
    """The FPR extraction runs awk inside a double-quoted command substitution.
    The awk program must not carry literal backslash-quotes: mawk rejects
    `$1==\\"fpr\\"` with 'backslash not last character on line', which made
    FPR empty and killed signing (`--local-user ""`). The pinned form uses a
    regex match so the program needs no embedded double quotes at all."""
    text = _read(APT_WORKFLOW)
    m = re.search(r"list-secret-keys \| awk -F: ([^\n]+)", text)
    assert m, "the FPR extraction must be present"
    program = m.group(1)
    assert "/^fpr:/" in program, f"awk program must match by /^fpr:/, got: {program!r}"
    assert "\\\"" not in program, f"no escaped quotes inside the awk program: {program!r}"


def test_public_key_file_exists_and_is_not_ignored():
    """The repo must carry the armored PUBLIC signing key, and .gitignore must
    not exclude it (it is committed, and the workflow copies it to gh-pages)."""
    assert PUBLIC_KEY.is_file(), "quota-manager.gpg must be committed at the repo root"
    text = _read(PUBLIC_KEY).lstrip()
    assert text.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"), \
        "quota-manager.gpg must be an armored PGP PUBLIC key (never the secret key)"
    gi = _read(REPO / ".gitignore")
    assert not re.search(r"(?m)^\*\.(gpg|asc|key)$", gi), \
        "a broad *.gpg/*.asc ignore rule would hide the committed public key"
    assert "quota-manager.gpg" not in gi, \
        ".gitignore must not name quota-manager.gpg"


# --------------------------------------------------------------------------- #
# Debian control metadata
# --------------------------------------------------------------------------- #

def test_control_template_fields():
    text = _read(PACKAGING / "DEBIAN" / "control.template")
    fields = {
        "Package": "quota-manager",
        "Version": "__VERSION__",  # rendered by the workflow
        "Architecture": "all",
        "Section": "net",
        "Priority": "optional",
        "Maintainer": "Youssef Alkhodary",
    }
    for key, val in fields.items():
        assert re.search(rf"^{key}:\s*{re.escape(val)}", text, re.M), \
            f"control.template must carry {key}: {val!r}"
    for dep in ("python3 (>= 3.10)", "python3-venv", "dnsmasq", "nftables",
                "iproute2", "kmod", "ppp", "ca-certificates"):
        assert dep in text, f"control.template Depends must include {dep}"
    assert "Description:" in text


# --------------------------------------------------------------------------- #
# postinst / prerm lifecycle scripts
# --------------------------------------------------------------------------- #

def test_postinst_is_a_root_executable_bash_script():
    p = PACKAGING / "DEBIAN" / "postinst"
    text = _read(p)
    assert text.startswith("#!/bin/bash")
    assert "set -euo pipefail" in text
    # the real work lives in the shared script every installer calls
    assert "scripts/post_install.sh deb" in text
    shared = _read(REPO / "scripts" / "post_install.sh")
    assert "QUOTA_NO_APT=1" in shared, "post-install must run setup without apt"
    assert "setup_gateway_kali.sh" in shared
    assert "systemctl" in shared
    assert "/opt/quota-manager" in shared
    assert shared.endswith("exit 0\n"), "post-install must end cleanly"
    assert text.endswith("exit 0\n"), "postinst must end cleanly"
    # executable bit is a Debian requirement — enforce the committed blob's mode
    assert _git_index_mode("packaging/DEBIAN/postinst") == "100755", \
        "postinst must be executable (git update-index --chmod=+x)"


def test_prerm_is_a_root_executable_bash_script():
    p = PACKAGING / "DEBIAN" / "prerm"
    text = _read(p)
    assert text.startswith("#!/bin/bash")
    assert "set -euo pipefail" in text
    assert "systemctl stop" in text
    assert "systemctl disable" in text
    assert text.endswith("exit 0\n")
    assert _git_index_mode("packaging/DEBIAN/prerm") == "100755", \
        "prerm must be executable (git update-index --chmod=+x)"


# --------------------------------------------------------------------------- #
# setup script: package installs must skip the apt block
# --------------------------------------------------------------------------- #

def test_setup_script_has_quota_no_apt_guard():
    text = _read(REPO / "scripts" / "setup_gateway_kali.sh")
    assert "QUOTA_NO_APT" in text, \
        "setup_gateway_kali.sh must honor QUOTA_NO_APT (postinst sets it)"


def test_setup_script_defines_cfg_history_log_before_use():
    """CFG_HISTORY_LOG is rendered into the quota-dnslog.conf fragment (step
    4.5) AND into config.yaml (step 6). The script runs `set -u`, so a late
    definition crashed every .deb install at the 4.5 heredoc with
    'CFG_HISTORY_LOG: unbound variable' — the assignment must come before the
    first use (0.1.4 hotfix)."""
    lines = _read(REPO / "scripts" / "setup_gateway_kali.sh").splitlines()
    assign = next(i for i, l in enumerate(lines)
                  if "CFG_HISTORY_LOG=" in l and "${CFG_HISTORY_LOG:-" in l)
    first_use = next(i for i, l in enumerate(lines)
                     if "log-facility=$CFG_HISTORY_LOG" in l)
    assert assign < first_use, (
        f"CFG_HISTORY_LOG is assigned at line {assign + 1} but first used at "
        f"line {first_use + 1} — with `set -u` the heredoc expansion aborts "
        "the install")


# --------------------------------------------------------------------------- #
# dnsmasq config: sequential IP allocation (no gapped leases)
# --------------------------------------------------------------------------- #

def test_dnsmasq_configs_allocate_sequential_ips():
    """Both config writers (setup_gateway_kali.sh + topology.sh) must set
    dhcp-sequential-ip in EVERY dhcp-range heredoc (WAN + LAN). Without it
    dnsmasq's default MAC-hash allocation spreads leases across the whole pool
    (e.g. .155 then .185) instead of filling contiguously from POOL_START."""
    for script in ("setup_gateway_kali.sh", "topology.sh"):
        text = _read(REPO / "scripts" / script)
        assert text.count("dhcp-range=$POOL_START") == 2, \
            f"{script} must write two IPv4 dnsmasq heredocs (WAN + LAN)"
        # the directive itself is a bare config line; the comment mentions it too
        seq_lines = re.findall(r"(?m)^dhcp-sequential-ip$", text)
        assert len(seq_lines) == 2, \
            f"{script} must set dhcp-sequential-ip in both heredocs"


# --------------------------------------------------------------------------- #
# .gitignore
# --------------------------------------------------------------------------- #

def test_deb_ignored():
    text = _read(REPO / ".gitignore")
    assert re.search(r"^\*\.deb$", text, re.M), ".gitignore must ignore *.deb"


def test_postinst_keeps_wan_mode_and_never_aborts_on_setup_failure():
    text = (REPO / "scripts" / "post_install.sh").read_text()
    # an update must not reset a dashboard-enabled WAN box to LAN
    assert "export QUOTA_TOPOLOGY=wan" in text
    assert "topology_source" in text and "chap-secrets" in text
    # a failing setup step must not leave the package half-configured
    assert "if ! APP_DIR=" in text
    assert text.index("if ! APP_DIR=") < text.index('systemctl enable "$SERVICE"')


# --------------------------------------------------------------------------- #
# multi-distro packaging (issue #15): shared post-install, tarball, rpm, arch
# --------------------------------------------------------------------------- #

def test_post_install_records_kind_and_restores_selinux_labels():
    text = _read(REPO / "scripts" / "post_install.sh")
    assert text.startswith("#!/bin/bash")
    assert '.install-kind' in text, "the updater picks its asset from this marker"
    assert "restorecon" in text
    assert text.index("restorecon") < text.index("QUOTA_NO_APT=1 bash")


def test_generic_installer_is_safe_and_calls_shared_post_install():
    text = _read(PACKAGING / "generic" / "install.sh")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "--no-deps" in text and "--uninstall" in text
    assert "systemctl" in text and "id -u" in text
    assert "post_install.sh\" tarball" in text
    # keeps the venv + data across upgrades: it never deletes them
    assert ".venv" not in text.replace("keep the venv", "")
    assert "/var/lib/quota-gateway" in text  # only mentioned in the kept-data message
    assert "rm -rf /var/lib" not in text


def test_rpm_spec_only_stops_service_on_real_erase():
    text = _read(PACKAGING / "rpm" / "quota-manager.spec.template")
    assert "BuildArch:      noarch" in text
    assert "post_install.sh rpm" in text
    pre = text.split("%preun", 1)[1].split("%postun", 1)[0]
    assert '"$1" -eq 0' in pre, "upgrade runs new %post before old %preun"
    for dep in ("dnsmasq", "nftables", "iproute", "ppp", "python3-pip"):
        assert dep in text


def test_arch_install_hooks_and_pkgbuild():
    hooks = _read(PACKAGING / "arch" / "quota-manager.install")
    for fn in ("post_install", "post_upgrade", "pre_upgrade", "pre_remove"):
        assert fn + "()" in hooks
    assert "post_install.sh arch" in hooks
    pkgbuild = _read(PACKAGING / "arch" / "PKGBUILD")
    assert "arch=('any')" in pkgbuild
    assert "install=quota-manager.install" in pkgbuild
    assert "iproute2" in pkgbuild and "python-pip" in pkgbuild


def test_release_workflow_builds_and_uploads_every_package_kind():
    wf = _read(REPO / ".github" / "workflows" / "release.yml")
    build = _read(PACKAGING / "build_packages.sh")
    for needle in ("quota-manager-${VERSION}.tar.gz", "rpmbuild -bb",
                   ".pkg.tar.zst", "dpkg-deb --build", "find \"$T\" -type l",
                   "chmod 755 \"$STAGE/DEBIAN/postinst\""):
        assert needle in build, needle
    for needle in ("packaging/build_packages.sh", "dist/quota-manager_*.deb",
                   "dist/quota-manager-*.tar.gz", "dist/quota-manager-*.rpm",
                   "dist/quota-manager-*.pkg.tar.zst"):
        assert needle in wf, needle


def test_packaging_smoke_workflow_installs_in_real_distro_containers():
    wf = yaml.safe_load(_read(REPO / ".github" / "workflows" / "packaging-smoke.yml"))
    include = wf["jobs"]["smoke"]["strategy"]["matrix"]["include"]
    images = {m["image"]: m["kind"] for m in include}
    assert images.get("fedora:latest") == "rpm"
    assert images.get("archlinux:latest") == "arch"
    assert images.get("debian:12") == "deb" and images.get("ubuntu:24.04") == "deb"
    smoke = _read(PACKAGING / "ci" / "smoke.sh")
    for needle in (".install-kind", "reinstall", "qm_net_pkgs", "removed cleanly"):
        assert needle in smoke
    # the build script must have no hidden runtime dependency on the release tag
    assert "GITHUB_REF" not in _read(PACKAGING / "build_packages.sh")


def test_generic_installer_accepts_every_flag_the_updater_passes():
    """The in-app updater runs `install.sh --upgrade` for tarball installs; an
    installer that rejects it makes every panel update fail (found in review)."""
    import re as _re
    updater = _read(REPO / "quota" / "updater.py")
    flags = set(_re.findall(r'"(--[a-z-]+)"\]', updater.split("else:  # tarball", 1)[1]
                            .split("argv =", 1)[0]))
    assert "--upgrade" in flags
    installer = _read(PACKAGING / "generic" / "install.sh")
    for f in flags:
        assert f + ")" in installer, f"install.sh must accept {f}"


def test_openwrt_installer_supports_apk_and_opkg():
    """OpenWrt 25.12 dropped opkg for apk - the docs/installer must not assume
    either (community report on the project's video)."""
    text = _read(REPO / "scripts" / "install_openwrt.sh")
    assert text.startswith("#!/bin/sh"), "BusyBox ash: no bash on a stock router"
    assert "apk add" in text and "opkg install" in text
    assert text.index("command -v apk") < text.index("command -v opkg")
    assert "bash" not in text.replace("BusyBox", "")
    for pkg in ("python3-pip", "nftables", "dnsmasq-full", "git-http", "ca-bundle"):
        assert pkg in text
    assert "pytest" in text, "dev-only packages must be filtered out on a router"
    for readme in ("README.md", "README_AR.md"):
        assert "install_openwrt.sh" in _read(REPO / readme)
    init = _read(REPO / "scripts" / "quota-manager.openwrt")
    assert ".venv/bin/python" in init
