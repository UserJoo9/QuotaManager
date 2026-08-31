"""Unit tests for quota/updater.py (version math, changelog parse, GitHub
check, and the .deb install flow) — no real network, no real dpkg."""

from __future__ import annotations

import asyncio

from quota.updater import Updater, parse_changelog, parse_version


class FakeDb:
    """Minimal settings store backing the Updater (get/set/add_event)."""

    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.events: list[str] = []

    async def get_setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    async def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    async def delete_setting(self, key: str) -> None:
        self.settings.pop(key, None)

    async def add_event(self, message: str, level: str = "info",
                        when: str | None = None) -> None:
        self.events.append(message)


RELEASE = {
    "tag_name": "v0.3.1",
    "body": "Release notes body.",
    "assets": [{"name": "quota-manager_0.3.1_all.deb",
                "browser_download_url": "https://example/deb"}],
}

CHANGELOG = """
# Changelog

## [Unreleased]
### Added
- work in progress

## [0.3.1]
### Fixed
- bug three.

## [0.3.0]
### Added
- feature two.

## [0.2.0]
### Added
- feature one.
"""


_cached_loop = None
def _get_loop():
    global _cached_loop
    if _cached_loop is None or _cached_loop.is_closed():
        _cached_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_cached_loop)
    return _cached_loop

def run(coro):
    return _get_loop().run_until_complete(coro)


# -- version math ------------------------------------------------------------

def test_parse_version_compares_dotted():
    assert parse_version("0.2.0") == (0, 2, 0)
    assert parse_version("0.10.0") > parse_version("0.9.9")
    assert parse_version("0.2.1") > parse_version("0.2.0")
    assert parse_version("v0.2.0") == parse_version("0.2.0")  # v-prefix ignored
    assert parse_version("garbage") == ()


def test_parse_changelog_returns_newer_sections_newest_first():
    sections = parse_changelog(CHANGELOG, "0.2.0", "0.3.1")
    assert [s["version"] for s in sections] == ["0.3.1", "0.3.0"]
    assert "bug three." in sections[0]["body"]
    # Unreleased is skipped even though it parses as a heading
    assert not any(s["version"].lower() == "unreleased" for s in sections)


def test_parse_changelog_filters_to_latest_bound():
    sections = parse_changelog(CHANGELOG, "0.2.0", "0.3.0")
    assert [s["version"] for s in sections] == ["0.3.0"]
    assert parse_changelog(CHANGELOG, "0.3.1", "0.3.1") == []


def test_parse_changelog_old_box_gets_every_thing():
    sections = parse_changelog(CHANGELOG, "0.1.0", "0.3.1")
    assert [s["version"] for s in sections] == ["0.3.1", "0.3.0", "0.2.0"]


def test_parse_changelog_matches_date_suffixed_headers():
    """The repo's CHANGELOG headers carry a `` — YYYY-MM-DD`` date suffix
    (``## [0.2.1] — 2026-08-17``). The live "Show details" popup showed
    "No changelog available" because the parser only matched bare
    ``## [0.2.1]`` headers — pin the real format so it can't regress."""
    text = """
# Changelog

## [Unreleased]

## [0.2.1] — 2026-08-17

### Added

- self-update checks.

### Fixed

- a bug.

## [0.2.0] — 2026-08-16

### Added

- older feature.
"""
    sections = parse_changelog(text, "0.2.0", "0.2.1")
    assert [s["version"] for s in sections] == ["0.2.1"]
    assert sections[0]["title"] == "v0.2.1"
    # the body keeps its "###" sub-headings — the popup renders from Added down
    assert "### Added" in sections[0]["body"]
    assert "self-update checks." in sections[0]["body"]
    # the version is still the only thing captured (never the date)
    assert sections[0]["version"] == "0.2.1"


# -- the updater -------------------------------------------------------------

def make_updater(db, **kw):
    return Updater(db, fetch_json=kw.get("fetch_json"),
                   fetch_text=kw.get("fetch_text"),
                   download=kw.get("download"),
                   run_command=kw.get("run_command"))


def test_check_finds_update_and_persists():
    db = FakeDb()
    calls = {"json": 0}
    up = Updater(
        db, current_version="0.2.0",
        fetch_json=lambda url, timeout=20: calls.__setitem__("json", calls["json"] + 1) or RELEASE,
        fetch_text=lambda url, timeout=20: CHANGELOG)
    st = run(up.check_now())
    assert st["available"] is True
    assert st["latest_version"] == "0.3.1"
    assert [c["version"] for c in st["changelog"]] == ["0.3.1", "0.3.0"]
    assert db.events and "Update available" in db.events[0]
    # persisted -> survives a fresh Updater (notification survives reload)
    st2 = run(Updater(db, current_version="0.2.0").state())
    assert st2["available"] is True


def test_check_up_to_date():
    db = FakeDb()
    up = Updater(db, current_version="0.3.1",
                 fetch_json=lambda url, timeout=20: RELEASE,
                 fetch_text=lambda url, timeout=20: CHANGELOG)
    st = run(up.check_now())
    assert st["available"] is False
    assert st["latest_version"] == "0.3.1"
    assert st["changelog"] == []


def test_check_failure_preserves_last_known():
    db = FakeDb()
    up = Updater(db, current_version="0.2.0",
                 fetch_json=lambda url, timeout=20: RELEASE)
    run(up.check_now())  # first check succeeds
    failing = Updater(db, current_version="0.2.0",
                      fetch_json=lambda url, timeout=20: (_ for _ in ()).throw(OSError("offline")))
    st = run(failing.check_now())
    assert st["available"] is True          # last known latest kept
    assert "offline" in st["error"]
    assert st["latest_version"] == "0.3.1"


def test_maybe_check_gated_by_interval_and_enabled():
    db = FakeDb()
    db.settings["updates_state"] = '{"checked_at": "2030-01-01T00:00:00+00:00", "latest_version": ""}'
    called = []
    up = Updater(db, current_version="0.2.0",
                 fetch_json=lambda url, timeout=20: called.append(1) or RELEASE)
    run(up.maybe_check())       # fresh checked_at -> no fetch
    assert called == []
    db.settings["updates_enabled"] = ""      # disabled -> no fetch either
    db.settings["updates_state"] = '{"checked_at": "2000-01-01T00:00:00+00:00"}'
    run(up.maybe_check())
    assert called == []
    db.settings["updates_enabled"] = "1"     # enabled + stale -> fetch
    run(up.maybe_check())
    assert called == [1]


def test_auto_install_fires_after_check():
    db = FakeDb()
    db.settings["updates_auto_install"] = "1"
    installed = []
    up = Updater(
        db, current_version="0.2.0",
        fetch_json=lambda url, timeout=20: RELEASE,
        fetch_text=lambda url, timeout=20: CHANGELOG,
        download=lambda url, dest, timeout=120: None,
        run_command=lambda argv, timeout=15: installed.append(argv) or (0, ""))
    st = run(up.check_now())
    assert st["available"] is True
    # let the background install task finish
    run(asyncio.sleep(0.05))
    assert installed, "auto-install should have downloaded + installed"
    st = run(up.state())
    assert st["available"] is False        # latest cleared after install
    assert st["last_install"] != ""
    assert any("Update v0.3.1 installed" in e for e in db.events)


def test_install_requires_available_update():
    db = FakeDb()
    up = Updater(db, current_version="0.2.0",
                 fetch_json=lambda url, timeout=20: {})
    res = run(up.install_latest())
    assert res == {"ok": False, "reason": "no update available"}


def test_install_deb_uses_systemd_run_then_apt_fallback():
    calls = []
    up = Updater("x", run_command=lambda argv, timeout=15: calls.append(argv) or (0, ""))
    ok, _out = up._install_deb("/tmp/q.deb")
    assert ok is True
    assert calls[0][0] == "systemd-run"
    # systemd-run missing -> falls back to plain apt-get
    fallback = Updater(
        "x", run_command=lambda argv, timeout=15:
            (1, "systemd-run: not found") if argv[0] == "systemd-run"
            else (0, "installed"))
    ok, _out = fallback._install_deb("/tmp/q.deb")
    assert ok is True


def test_install_uses_release_asset_url():
    db = FakeDb()
    up = Updater(db, current_version="0.2.0",
                 fetch_json=lambda url, timeout=20: RELEASE)
    assert up._find_deb_url(RELEASE, "0.3.1") == "https://example/deb"
    # no assets -> constructed URL fallback
    assert up._find_deb_url({}, "0.3.1") == (
        "https://github.com/UserJoo9/QuotaManager/releases/download/"
        "v0.3.1/quota-manager_0.3.1_all.deb")


def test_check_now_refuses_when_disabled():
    """The "check automatically" toggle is the master switch: a disabled box
    never dials GitHub (no new error persisted), the card tells the admin to
    enable it instead of showing a stale failure."""
    db = FakeDb()
    db.settings["updates_enabled"] = ""     # toggle OFF
    db.settings["updates_state"] = ('{"checked_at": "2026-08-17T00:00:00+00:00", '
                                    '"error": "urlopen error timed out"}')
    def explode(*a, **k):
        raise AssertionError("disabled updater must not fetch")
    up = Updater(db, current_version="0.2.0",
                 fetch_json=explode, fetch_text=explode)
    st = run(up.check_now())
    assert st["enabled"] is False
    assert st["error"] == ""                 # stale error not surfaced either
    assert st["latest_version"] == ""
    # the factual last-check time is kept (the UI hides it while OFF)
    assert st["checked_at"] == "2026-08-17T00:00:00+00:00"


def test_set_enabled_clears_stale_error_on_enable():
    """Turning the check ON wipes a stale last-error so the card starts clean;
    turning it OFF leaves the record alone (the UI hides it anyway)."""
    db = FakeDb()
    db.settings["updates_state"] = ('{"checked_at": "2026-08-17T00:00:00+00:00", '
                                    '"latest_version": "0.3.1", '
                                    '"error": "urlopen error timed out"}')
    up = Updater(db, current_version="0.2.0")
    run(up.set_enabled(False))
    assert "urlopen error timed out" in db.settings["updates_state"], \
        "disabling must not destroy the record"
    run(up.set_enabled(True))
    assert '"error": ""' in db.settings["updates_state"], \
        "enabling must clear the stale error"
    # the fresh state() no longer reports the old failure
    st = run(up.state())
    assert st["enabled"] is True
    assert st["error"] == ""