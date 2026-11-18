"""quota/apt_source.py: moving the apt source to the project's new address
once it is live (the GitHub repository rename). No network, no real /etc."""

from __future__ import annotations

import os
import stat

from quota import apt_source as A

LEGACY = A.LEGACY_BASE
NEW = A.NEW_BASE
LIVE = "Origin: Quota Manager\nSuite: stable\nCodename: stable\n"

DEB = ("deb [signed-by=/etc/apt/keyrings/quota-manager.gpg] "
       "{base} stable main\n")


def _write(d, name, text):
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def _never(url):
    raise AssertionError(f"must not touch the network: {url}")


def test_nothing_names_the_legacy_address_no_network_no_writes(tmp_path):
    _write(tmp_path, "other.list", "deb http://deb.debian.org/debian stable main\n")
    res = A.migrate_apt_source(sources_dir=str(tmp_path), fetch=_never)
    assert res == {"changed": False, "files": [], "reason": "not_configured"}


def test_missing_sources_dir_is_a_quiet_noop(tmp_path):
    res = A.migrate_apt_source(sources_dir=str(tmp_path / "nope"), fetch=_never)
    assert res["reason"] == "not_configured"


def test_switches_once_the_new_address_serves_the_repository(tmp_path):
    p = _write(tmp_path, "quota-manager.list", DEB.format(base=LEGACY))
    p.chmod(0o640)
    asked = []

    def fetch(url):
        asked.append(url)
        return LIVE

    res = A.migrate_apt_source(sources_dir=str(tmp_path), fetch=fetch)
    assert res["changed"] and res["reason"] == "migrated"
    assert p.read_text() == DEB.format(base=NEW)     # only the URL changed
    assert asked == [NEW + "/dists/stable/Release"]
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640   # permissions kept
    assert [f.name for f in tmp_path.iterdir()] == ["quota-manager.list"]  # no temp left


def test_second_run_is_a_noop(tmp_path):
    _write(tmp_path, "quota-manager.list", DEB.format(base=LEGACY))
    A.migrate_apt_source(sources_dir=str(tmp_path), fetch=lambda u: LIVE)
    res = A.migrate_apt_source(sources_dir=str(tmp_path), fetch=_never)
    assert res == {"changed": False, "files": [], "reason": "not_configured"}


def test_keeps_the_legacy_address_until_the_new_one_is_live(tmp_path):
    p = _write(tmp_path, "quota-manager.list", DEB.format(base=LEGACY))

    def not_found(url):
        raise OSError("HTTP Error 404: Not Found")

    res = A.migrate_apt_source(sources_dir=str(tmp_path), fetch=not_found)
    assert res["reason"] == "new_repo_not_live" and not res["changed"]
    assert p.read_text() == DEB.format(base=LEGACY)


def test_a_page_that_is_not_the_signed_repository_does_not_count(tmp_path):
    p = _write(tmp_path, "quota-manager.list", DEB.format(base=LEGACY))
    res = A.migrate_apt_source(sources_dir=str(tmp_path),
                               fetch=lambda u: "<html>GitHub Pages 404</html>")
    assert res["reason"] == "new_repo_not_live"
    assert p.read_text() == DEB.format(base=LEGACY)


def test_deb822_sources_file_and_case_insensitive_host(tmp_path):
    text = ("Types: deb\nURIs: https://USERJOO9.github.io/QuotaManager/\n"
            "Suites: stable\nComponents: main\n"
            "Signed-By: /etc/apt/keyrings/quota-manager.gpg\n")
    p = _write(tmp_path, "quota-manager.sources", text)
    res = A.migrate_apt_source(sources_dir=str(tmp_path), fetch=lambda u: LIVE)
    assert res["changed"]
    out = p.read_text()
    assert out.startswith("Types: deb\nURIs: " + NEW + "/\n")
    assert "Signed-By: /etc/apt/keyrings/quota-manager.gpg" in out


def test_a_longer_name_that_merely_starts_the_same_is_left_alone(tmp_path):
    other = "https://UserJoo9.github.io/QuotaManager2"
    p = _write(tmp_path, "x.list", DEB.format(base=other))
    res = A.migrate_apt_source(sources_dir=str(tmp_path), fetch=_never)
    assert res["reason"] == "not_configured"
    assert p.read_text() == DEB.format(base=other)


def test_other_files_and_other_extensions_are_untouched(tmp_path):
    keep = _write(tmp_path, "quota-manager.list.bak", DEB.format(base=LEGACY))
    mine = _write(tmp_path, "a.list", DEB.format(base=LEGACY))
    other = _write(tmp_path, "b.list", "deb http://example.org stable main\n")
    A.migrate_apt_source(sources_dir=str(tmp_path), fetch=lambda u: LIVE)
    assert keep.read_text() == DEB.format(base=LEGACY)
    assert other.read_text() == "deb http://example.org stable main\n"
    assert mine.read_text() == DEB.format(base=NEW)


def test_pages_base_and_default_addresses():
    assert LEGACY == "https://UserJoo9.github.io/QuotaManager"
    assert NEW == "https://UserJoo9.github.io/Netrex"
    assert A.pages_base("o/r") == "https://o.github.io/r"
