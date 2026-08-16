"""Tests for the tun2socks auto-provisioner (quota/tun2socks.py).

Fake ``ss`` / binary probe / downloader / spawner keep the tests root-free
and off the network: we assert the download+verify+spawn decision sequence,
not kernel or GitHub behaviour (mirrors FakeIp / FakeTc in the other
kernel-side test files).
"""

from __future__ import annotations

import os
import platform
import zipfile
from pathlib import Path

import pytest

import quota.tun2socks as tsmod
from core.config import Config
from quota.tun2socks import (RESPAWN_GATE, RETRY_GATE, STATE_DOWNLOADING,
                             STATE_ERROR, STATE_NO_BINARY, STATE_NO_PROXY,
                             STATE_OFF, STATE_RUNNING, Tun2socksManager)


@pytest.fixture(autouse=True)
def _fake_proxy_probe(monkeypatch):
    """Tests never touch the network: the real probe does a TCP connect.
    The probe itself has dedicated tests below with an explicit injectable."""
    monkeypatch.setattr(tsmod, "_default_proxy_probe", lambda proxy: True)


def _make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def make_cfg(tmp_path: Path, *, socks: str = "127.0.0.1:10808") -> Config:
    cfg = Config()
    cfg.vpn_share.tun2socks = True
    cfg.vpn_share.socks_proxy = socks
    cfg.vpn_share.binary = str(tmp_path / "bin" / "tun2socks")
    return cfg


class FakeRun:
    """Scriptable ``ss``/``--version`` runner. Default: success + empty out."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.script: dict[tuple[str, ...], tuple[int, str]] = {}

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        return self.script.get(tuple(argv), (0, ""))

    def set(self, out: str, rc: int = 0, *argv: str) -> None:
        self.script[tuple(argv)] = (rc, out)


class FakeChild:
    def __init__(self, rc: int | None = None) -> None:
        self._rc = rc
        self.stopped = False

    def poll(self) -> int | None:
        return self._rc

    def stop(self) -> None:
        self.stopped = True


class FakeSpawn:
    def __init__(self, rc: int | None = None, raise_: Exception | None = None
                 ) -> None:
        self.calls: list[list[str]] = []
        self.child = FakeChild(rc)
        self.raise_ = raise_

    def __call__(self, argv: list[str]) -> FakeChild:
        self.calls.append(argv)
        if self.raise_ is not None:
            raise self.raise_
        return self.child


class FakeDownload:
    def __init__(self, ok: bool = True, message: str = "ok") -> None:
        self.calls: list[tuple[str, str, Path]] = []
        self.ok = ok
        self.message = message

    def __call__(self, url: str, sha256: str, dest: Path) -> tuple[bool, str]:
        self.calls.append((url, sha256, dest))
        return self.ok, self.message


def _ready_binary(tmp_path: Path) -> Path:
    """A real, executable file so _binary_is_ready() passes the probe."""
    p = Path(tmp_path / "bin" / "tun2socks")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


class FakeClock:
    """Injectable clock so the download/spawn retry gates can be advanced."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


SS_LINE = ("LISTEN 0 4096 127.0.0.1:10808 0.0.0.0:* "
           'users:(("v2rayN",pid=1234,fd=21))')


def _ss(run: FakeRun, line: str = SS_LINE, rc: int = 0) -> None:
    run.set(line, rc, "ss", "-tlnp")


# ---------------------------------------------------------------------------
# reconcile(False) / basic state machine
# ---------------------------------------------------------------------------

def test_reconcile_off_stops_child_and_reports_off(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ready_binary(tmp_path)
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_RUNNING
    assert mgr._child is not None
    off = mgr.reconcile(False)
    assert off.state == STATE_OFF
    assert spawn.child.stopped


def test_reconcile_off_with_no_child_is_a_noop(tmp_path):
    cfg = make_cfg(tmp_path)
    mgr = Tun2socksManager(cfg, verify_delay=0.0)
    assert mgr.reconcile(False).state == STATE_OFF
    assert mgr.reconcile(False).state == STATE_OFF


def test_running_status_surfaces_proxy_and_interface(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)
    _ready_binary(tmp_path)
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_RUNNING
    assert status.proxy == "127.0.0.1:10808"
    assert status.interface == "tun0"
    assert "tun0" in status.message


# ---------------------------------------------------------------------------
# spawn argv + idempotence
# ---------------------------------------------------------------------------

def test_spawns_with_detected_proxy_and_tun_args(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)
    _ready_binary(tmp_path)
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           verify_delay=0.0)
    mgr.reconcile(True)
    assert spawn.calls == [[
        str(cfg.vpn_share.binary), "-device", "tun0", "-proxy",
        "socks5://127.0.0.1:10808"]]
    # tun2socks v2 has no -tun-ip/-tun-gw flags (an undefined flag makes it
    # exit immediately) — the address is applied via _configure_interface.
    assert ["ip", "addr", "add", "10.0.0.1/24", "dev", "tun0"] in run.calls
    assert ["ip", "link", "set", "dev", "tun0", "up"] in run.calls


def test_does_not_respawn_while_child_alive(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ready_binary(tmp_path)
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           verify_delay=0.0)
    mgr.reconcile(True)
    mgr.reconcile(True)
    assert len(spawn.calls) == 1


def test_proxy_falls_back_to_config_when_no_listener_matches(tmp_path):
    cfg = make_cfg(tmp_path, socks="127.0.0.1:9999")
    run = FakeRun()
    _ss(run, line="LISTEN 0 5 127.0.0.1:8080 0.0.0.0:* "
                  'users:(("nginx",pid=1,fd=3))')
    _ready_binary(tmp_path)
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_RUNNING
    assert "-proxy" in spawn.calls[0]
    assert spawn.calls[0][spawn.calls[0].index("-proxy") + 1] == \
        "socks5://127.0.0.1:9999"


def test_no_proxy_reported_when_ss_unavailable_and_no_fallback(tmp_path):
    cfg = make_cfg(tmp_path, socks="")
    run = FakeRun()
    run.set("", 1, "ss", "-tlnp")  # ss missing
    _ready_binary(tmp_path)
    mgr = Tun2socksManager(cfg, run_command=run, verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_NO_PROXY
    assert "proxy" in status.message


def test_dead_fallback_proxy_never_spawns_blackhole(tmp_path):
    """When no SOCKS listener matches and the configured fallback is NOT
    actually listening, the bridge must NOT spawn into it (a dead endpoint
    would silently drop every client packet — the live-box blackhole)."""
    cfg = make_cfg(tmp_path, socks="127.0.0.1:10808")
    run = FakeRun()
    _ss(run, line="LISTEN 0 5 127.0.0.1:8080 0.0.0.0:* "
                  'users:(("nginx",pid=1,fd=3))')
    _ready_binary(tmp_path)
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           proxy_probe=lambda p: False, verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_NO_PROXY
    assert "10808" in status.message
    assert spawn.calls == []  # nothing was spawned
    assert mgr._child is None


def test_live_fallback_proxy_still_spawns(tmp_path):
    """The probe only gates a DEAD fallback — a live configured endpoint
    behaves exactly as before (spawned, running)."""
    cfg = make_cfg(tmp_path, socks="127.0.0.1:10808")
    run = FakeRun()
    _ss(run, line="LISTEN 0 5 127.0.0.1:8080 0.0.0.0:* "
                  'users:(("nginx",pid=1,fd=3))')
    _ready_binary(tmp_path)
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           proxy_probe=lambda p: True, verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_RUNNING
    assert len(spawn.calls) == 1
    assert spawn.calls[0][spawn.calls[0].index("-proxy") + 1] == \
        "socks5://127.0.0.1:10808"


def test_detected_proxy_skips_probe(tmp_path):
    """A proxy found live via `ss -tlnp` is by definition listening — the
    bridge is spawned even when the probe is rigged to fail."""
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)  # v2rayN listening on 127.0.0.1:10808
    _ready_binary(tmp_path)
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           proxy_probe=lambda p: False, verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_RUNNING
    assert len(spawn.calls) == 1


# ---------------------------------------------------------------------------
# binary provisioning (download + verify)
# ---------------------------------------------------------------------------

def test_default_download_extracts_goreleaser_named_binary(tmp_path):
    """The real downloader must accept the goreleaser archive layout, where
    the binary is named `tun2socks-<os>-<arch>` (e.g. tun2socks-linux-amd64),
    not a bare `tun2socks`. Regression: v2.7.0 ships the suffixed name, so the
    old exact-match extractor failed with "archive has no tun2socks binary"."""
    import hashlib
    payload = b"\x7fELF-binary-bytes"
    zippath = tmp_path / "tun2socks-linux-amd64.zip"
    _make_zip(zippath, {"tun2socks-linux-amd64": payload})
    sha = hashlib.sha256(zippath.read_bytes()).hexdigest()
    dest = tmp_path / "bin" / "tun2socks"
    ok, msg = tsmod._default_download(zippath.resolve().as_uri(), sha, dest)
    assert ok, msg
    assert dest.read_bytes() == payload
    if os.name != "nt":  # chmod exec-bit is a no-op on Windows
        assert dest.stat().st_mode & 0o111


def test_default_download_accepts_bare_name_too(tmp_path):
    """The extractor still accepts the historical bare `tun2socks` member."""
    import hashlib
    payload = b"\x7fELF-binary-bytes"
    zippath = tmp_path / "t.zip"
    _make_zip(zippath, {"tun2socks": payload})
    sha = hashlib.sha256(zippath.read_bytes()).hexdigest()
    dest = tmp_path / "bin" / "tun2socks"
    ok, msg = tsmod._default_download(zippath.resolve().as_uri(), sha, dest)
    assert ok, msg
    assert dest.read_bytes() == payload


def test_default_download_reports_missing_binary(tmp_path):
    import hashlib
    zippath = tmp_path / "t.zip"
    _make_zip(zippath, {"README.md": b"no binary in here"})
    sha = hashlib.sha256(zippath.read_bytes()).hexdigest()
    ok, msg = tsmod._default_download(zippath.resolve().as_uri(), sha,
                                      tmp_path / "bin" / "tun2socks")
    assert not ok
    assert "no tun2socks binary" in msg


def test_downloads_pinned_binary_when_missing(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)
    download = FakeDownload()
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, download=download,
                           spawn=spawn, verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_RUNNING
    url, sha256, dest = download.calls[0]
    suffix = tsmod.ARCH_SUFFIX[platform.machine().lower()]
    assert url == f"{tsmod.RELEASE_BASE}{suffix}.zip"
    assert "v2.7.0" in url
    assert sha256 == tsmod.ARCH_SHA256[suffix]
    assert str(dest) == cfg.vpn_share.binary
    assert len(spawn.calls) == 1  # installed -> spawned


def test_download_happens_once_then_binary_is_cached(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)
    download = FakeDownload()
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, download=download,
                           spawn=spawn, verify_delay=0.0)
    mgr.reconcile(True)
    # binary still missing on disk (fake downloader wrote nothing) -> the
    # RETRY_GATE keeps the next tick from hitting GitHub again
    mgr.reconcile(True)
    assert len(download.calls) == 1


def test_download_failure_reports_no_binary_and_retries_after_gate(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)
    download = FakeDownload(ok=False, message="sha256 mismatch")
    spawn = FakeSpawn()
    clock = FakeClock()
    mgr = Tun2socksManager(cfg, run_command=run, download=download,
                           spawn=spawn, verify_delay=0.0, clock=clock)
    status = mgr.reconcile(True)
    assert status.state == STATE_NO_BINARY
    assert "sha256 mismatch" in status.message
    assert spawn.calls == []  # never spawns an unverified binary
    mgr.reconcile(True)  # gated: no second attempt
    assert len(download.calls) == 1
    # advance the clock past the retry gate -> attempt again
    clock.advance(RETRY_GATE + 1)
    mgr.reconcile(True)
    assert len(download.calls) == 2


def test_rejects_unsupported_architecture_without_downloading(tmp_path,
                                                              monkeypatch):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)
    download = FakeDownload()
    monkeypatch.setattr(tsmod.platform, "machine", lambda: "i386")
    mgr = Tun2socksManager(cfg, run_command=run, download=download,
                           verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_NO_BINARY
    assert "unsupported architecture" in status.message
    assert download.calls == []


def test_rejects_unverified_binary_when_no_pinned_checksum(tmp_path,
                                                           monkeypatch):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)
    download = FakeDownload()
    monkeypatch.setattr(tsmod.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(tsmod, "ARCH_SHA256", {})
    mgr = Tun2socksManager(cfg, run_command=run, download=download,
                           verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_NO_BINARY
    assert "unverified" in status.message
    assert download.calls == []


def test_downloading_status_is_reported_before_install(tmp_path):
    """While the download runs the UI must read an honest "installing…"
    state, not a dead no-binary line — capture the status the download
    callback observes mid-flight."""
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)
    captured: list[str] = []

    def recording_download(url: str, sha256: str, dest: Path):
        captured.append(mgr._status.state)
        return True, "ok"

    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, download=recording_download,
                           spawn=spawn, verify_delay=0.0)
    status = mgr.reconcile(True)
    assert captured == [STATE_DOWNLOADING]
    assert status.state == STATE_RUNNING  # install succeeded -> running


def test_binary_probe_failure_treated_as_missing(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ss(run)
    p = _ready_binary(tmp_path)
    run.set("", 1, str(p), "--version")  # binary exists but won't run
    download = FakeDownload()
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, download=download,
                           spawn=spawn, verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_RUNNING
    assert len(download.calls) == 1  # re-provisioned


# ---------------------------------------------------------------------------
# child lifecycle
# ---------------------------------------------------------------------------

def test_dead_child_reports_error_and_respawn_is_gated(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ready_binary(tmp_path)
    spawn = FakeSpawn(rc=1)  # child dies immediately
    clock = FakeClock()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           verify_delay=0.0, clock=clock)
    status = mgr.reconcile(True)
    assert status.state == STATE_ERROR
    assert "exited" in status.message
    mgr.reconcile(True)  # gated: no spawn loop
    assert len(spawn.calls) == 1
    clock.advance(RESPAWN_GATE + 1)
    status = mgr.reconcile(True)
    assert len(spawn.calls) == 2


def test_spawn_failure_reports_error(tmp_path):
    cfg = make_cfg(tmp_path)
    run = FakeRun()
    _ready_binary(tmp_path)
    spawn = FakeSpawn(raise_=OSError("boom"))
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           verify_delay=0.0)
    status = mgr.reconcile(True)
    assert status.state == STATE_ERROR
    assert "spawn failed" in status.message


def test_config_overrides_flow_into_manager(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.vpn_share.tun_interface = "utun9"
    cfg.vpn_share.tun_ip = "10.9.0.1"
    cfg.vpn_share.tun_gw = "10.9.0.2"
    cfg.vpn_share.download_url = "https://example.invalid/x.zip"
    cfg.vpn_share.download_sha256 = "abcd" * 16
    run = FakeRun()
    _ready_binary(tmp_path)
    spawn = FakeSpawn()
    mgr = Tun2socksManager(cfg, run_command=run, spawn=spawn,
                           verify_delay=0.0)
    mgr.reconcile(True)
    argv = spawn.calls[0]
    assert "-device" in argv and argv[argv.index("-device") + 1] == "utun9"
    # v2 CLI: no -tun-ip/-tun-gw flags; the configured IP goes via _configure_interface
    assert ["ip", "addr", "add", "10.9.0.1/24", "dev", "utun9"] in run.calls
    assert mgr.download_url == "https://example.invalid/x.zip"
    assert mgr.download_sha256 == "abcd" * 16