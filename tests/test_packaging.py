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
    assert "files: dist/quota-manager_*.deb" in text
    assert "permissions:" in text and "contents: write" in text


def test_release_workflow_verifies_tag_matches_version():
    text = _read(WORKFLOW)
    assert "quota/version.py" in text, "version must be read from quota/version.py"
    assert "GITHUB_REF" in text and "refs/tags/" in text, \
        "the tag must be checked against the version so a wrong tag fails loudly"


def test_release_workflow_ships_runtime_payload_only():
    text = _read(WORKFLOW)
    # payload allowlist: runtime dirs/files, never tests/source-control junk
    for needle in ("core quota api web scripts", "requirements-linux.txt"):
        assert needle in text
    assert ".git" not in text.replace(".github", ""), \
        ".git must not be staged into the package"


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
    assert "QUOTA_NO_APT=1" in text, "postinst must run setup without apt"
    assert "setup_gateway_kali.sh" in text
    assert "systemctl" in text
    assert "/opt/quota-manager" in text
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


# --------------------------------------------------------------------------- #
# .gitignore
# --------------------------------------------------------------------------- #

def test_deb_ignored():
    text = _read(REPO / ".gitignore")
    assert re.search(r"^\*\.deb$", text, re.M), ".gitignore must ignore *.deb"
