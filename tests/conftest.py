import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import aiosqlite.core
import pytest

from quota import updater

# Ensure all aiosqlite worker threads are daemon threads so unclosed connections
# in tests never block Python process shutdown on exit.
_orig_connection_init = aiosqlite.core.Connection.__init__

def _daemon_connection_init(self, *args, **kwargs):
    _orig_connection_init(self, *args, **kwargs)
    self._thread.daemon = True

aiosqlite.core.Connection.__init__ = _daemon_connection_init


@pytest.fixture(scope="session")
def release_signer():
    """Use real OpenPGP verification with an isolated, disposable key."""
    if not all(shutil.which(tool) for tool in ("gpg", "gpgv")):
        pytest.skip("GnuPG tools unavailable")
    # Keep the path short enough for GnuPG's Unix domain sockets on macOS.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="qm-test-gpg-") as directory:
        home = Path(directory)
        gpg = ["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback"]
        key = subprocess.run(
            [*gpg, "--passphrase", "", "--quick-generate-key", "Test release signer",
             "ed25519", "sign", "0"],
            capture_output=True, text=True, timeout=20,
        )
        assert key.returncode == 0, key.stderr
        keyring = home / "test.gpg"
        keyring.write_bytes(subprocess.check_output([*gpg, "--export"], timeout=20))
        yield gpg, keyring
        subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                       check=False, capture_output=True, timeout=20)


@pytest.fixture
def signed_release(tmp_path, monkeypatch, release_signer):
    gpg, keyring = release_signer
    monkeypatch.setattr(updater, "RELEASE_KEYRING", keyring)
    filename = "netrex_0.3.1_all.deb"
    payload = b"test package bytes"
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_text(f"{hashlib.sha256(payload).hexdigest()}  {filename}\n", encoding="ascii")
    subprocess.run([*gpg, "--detach-sign", str(manifest)], check=True,
                   capture_output=True, timeout=20)
    assets = {
        filename: payload,
        manifest.name: manifest.read_bytes(),
        "SHA256SUMS.sig": manifest.with_suffix(".sig").read_bytes(),
    }
    return assets
