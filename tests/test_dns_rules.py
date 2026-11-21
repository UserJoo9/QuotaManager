from __future__ import annotations

import asyncio
_cached_loop = None
def _get_loop():
    global _cached_loop
    if _cached_loop is None or _cached_loop.is_closed():
        _cached_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_cached_loop)
    return _cached_loop
"""Tests for quota/dns_rules.py: hosts/AdBlock-Plus parsing, wildcard
normalization, and the dnsmasq tag/rule renderer + file-writing manager.

Pure-function tests (parsing, normalization, rendering) need no root, no
dnsmasq, and no network — they only assert on generated text. The
DnsRuleManager tests use a tmp_path conf_dir and reload_dnsmasq=False so they
never shell out to `dnsmasq`/`systemctl` (mirrors how test_shaping.py avoids
touching the real `tc` binary).
"""


import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from quota import dns_rules as dr


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_parse_hosts_format_extracts_domains():
    text = (
        "# comment\n"
        "0.0.0.0 ads.example.com\n"
        "127.0.0.1 tracker.example.net  # inline comment\n"
        "0.0.0.0 localhost\n"          # must be skipped
        "not a hosts line\n"
        "\n"
    )
    assert dr.parse_hosts_format(text) == {"ads.example.com", "tracker.example.net"}


def test_parse_adblock_plus_extracts_domain_rules_only():
    text = (
        "! this is a comment\n"
        "[Adblock Plus 2.0]\n"
        "||ads.example.com^\n"
        "||tracker.example.net^$third-party\n"
        "@@||safe.example.com^\n"          # exception — must NOT be blocked
        "##.ad-banner\n"                    # element-hiding — no DNS equivalent
        "/some/path/rule\n"                 # path rule — no DNS equivalent
    )
    assert dr.parse_adblock_plus(text) == {"ads.example.com", "tracker.example.net"}


def test_compile_source_text_auto_detects_format():
    hosts_text = "0.0.0.0 ads.example.com\n"
    abp_text = "||ads.example.com^\n"
    assert dr.compile_source_text(hosts_text, "auto") == {"ads.example.com"}
    assert dr.compile_source_text(abp_text, "auto") == {"ads.example.com"}


# ---------------------------------------------------------------------------
# Wildcard / pattern normalization
# ---------------------------------------------------------------------------

def test_normalize_pattern_strips_leading_wildcard():
    # dnsmasq's domain match already includes every subdomain, so "*." is
    # just a no-op on top of the plain-domain match — stripped, not kept.
    assert dr.normalize_pattern("*.example.com") == "example.com"
    assert dr.normalize_pattern("EXAMPLE.com.") == "example.com"


def test_normalize_pattern_rejects_midlabel_wildcard():
    # Not something dnsmasq's exact-domain match can enforce.
    with pytest.raises(ValueError):
        dr.normalize_pattern("*.ads.*.example.com")
    with pytest.raises(ValueError):
        dr.normalize_pattern("ads*.example.com")


def test_normalize_pattern_rejects_empty_or_garbage():
    with pytest.raises(ValueError):
        dr.normalize_pattern("")
    with pytest.raises(ValueError):
        dr.normalize_pattern("not a domain!!")


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

@dataclass
class _FakeDevice:
    id: int
    mac: str


@dataclass
class _FakeRule:
    scope: str
    scope_id: Optional[int]
    action: str
    domain: str
    target_ip: Optional[str] = None
    enabled: bool = True


def test_render_tags_binds_every_device_mac():
    devices = [_FakeDevice(1, "aa:bb:cc:dd:ee:01"), _FakeDevice(2, "aa:bb:cc:dd:ee:02")]
    text = dr.render_tags(devices)
    assert "dhcp-host=aa:bb:cc:dd:ee:01,set:qmdev1" in text
    assert "dhcp-host=aa:bb:cc:dd:ee:02,set:qmdev2" in text


def test_render_rules_global_block_has_no_tag_prefix():
    rules = [_FakeRule("global", None, "block", "ads.example.com")]
    text = dr.render_rules(rules, [], {})
    assert "address=/ads.example.com/0.0.0.0" in text
    assert "tag:" not in text


def test_render_rules_device_scope_is_not_rendered():
    # dnsmasq has no per-client address=/server=; "tag:qmdev7 address=..."
    # is a syntax error that stops dnsmasq from starting (issue #41).
    rules = [_FakeRule("device", 7, "block", "tiktok.com")]
    text = dr.render_rules(rules, [], {})
    assert "tiktok.com" not in text
    assert not any(line.startswith("tag:") for line in text.splitlines())


def test_render_rules_user_scope_is_not_rendered():
    rules = [_FakeRule("user", 42, "block", "netflix.com")]
    text = dr.render_rules(rules, [], {42: [1, 2, 3]})
    assert "netflix.com" not in text


def test_render_rules_redirect_uses_target_ip():
    rules = [_FakeRule("global", None, "redirect", "printer.local", target_ip="192.168.2.50")]
    text = dr.render_rules(rules, [], {})
    assert "address=/printer.local/192.168.2.50" in text


def test_render_rules_rejects_config_injection_in_target_ip():
    # Defense in depth: even if a malformed target_ip somehow reaches the
    # renderer (the API layer should already reject it — see
    # test_api_rejects_config_injection_in_target_ip below), it must never
    # be written verbatim into a line dnsmasq will parse as two directives.
    rules = [_FakeRule("global", None, "redirect", "evil.example.com",
                       target_ip="1.1.1.1\nno-resolv")]
    text = dr.render_rules(rules, [], {})
    assert "no-resolv" not in text
    assert "evil.example.com" not in text  # the whole rule is dropped, not truncated


def test_render_rules_rejects_config_injection_in_dns_server():
    text = dr.render_rules([], [("device", 5, "1.1.1.1\nserver=evil")], {})
    assert "evil" not in text


def test_render_rules_disabled_rule_is_skipped():
    rules = [_FakeRule("global", None, "block", "ads.example.com", enabled=False)]
    text = dr.render_rules(rules, [], {})
    assert "ads.example.com" not in text


def test_render_rules_global_allow_renders_after_global_block():
    rules = [
        _FakeRule("global", None, "allow", "example.com"),
        _FakeRule("global", None, "block", "example.com"),
    ]
    text = dr.render_rules(rules, [], {})
    assert text.index("address=/example.com/0.0.0.0") < text.index("server=/example.com/#")


def test_render_rules_dns_server_overrides():
    text = dr.render_rules([], [("device", 5, "1.1.1.1"), ("global", None, "9.9.9.9")], {})
    assert "dhcp-option=tag:qmdev5,option:dns-server,1.1.1.1" in text
    assert "server=9.9.9.9" in text


def test_render_rules_user_dns_servers_merge_into_one_option_per_device():
    servers = [("user", 42, "1.1.1.3"), ("user", 42, "1.0.0.3"), ("user", 42, "1.1.1.3")]
    text = dr.render_rules([], servers, {42: [1, 2]})
    for dev_id in (1, 2):
        assert text.count(f"dhcp-option=tag:qmdev{dev_id},") == 1
        assert f"dhcp-option=tag:qmdev{dev_id},option:dns-server,1.1.1.3,1.0.0.3" in text


@pytest.mark.skipif(shutil.which("dnsmasq") is None, reason="dnsmasq not installed")
def test_rendered_rules_pass_real_dnsmasq_syntax_check(tmp_path: Path):
    rules = [
        _FakeRule("global", None, "block", "ads.example.com"),
        _FakeRule("global", None, "allow", "ok.example.com"),
        _FakeRule("global", None, "redirect", "printer.local", target_ip="192.168.2.50"),
        _FakeRule("device", 7, "block", "tiktok.com"),
        _FakeRule("user", 42, "allow", "x.com"),
    ]
    servers = [("user", 42, "1.1.1.3"), ("device", 5, "9.9.9.9"), ("global", None, "8.8.4.4")]
    conf = tmp_path / "quota-domains.conf"
    conf.write_text(dr.render_rules(rules, servers, {42: [1, 2]}))
    probe = subprocess.run(["dnsmasq", "--test", "-C", str(conf)],
                           capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr


# ---------------------------------------------------------------------------
# DnsRuleManager (file writing, no real dnsmasq/systemctl involved)
# ---------------------------------------------------------------------------

def test_manager_writes_both_files(tmp_path: Path):
    manager = dr.DnsRuleManager(conf_dir=str(tmp_path), reload_dnsmasq=False)
    devices = [_FakeDevice(1, "aa:bb:cc:dd:ee:01")]
    rules = [_FakeRule("global", None, "block", "ads.example.com")]
    changed = manager.apply(devices, rules, [], {})
    assert changed is True
    assert (tmp_path / "quota-tags.conf").exists()
    assert (tmp_path / "quota-domains.conf").exists()
    assert "qmdev1" in (tmp_path / "quota-tags.conf").read_text()
    assert "ads.example.com" in (tmp_path / "quota-domains.conf").read_text()


def test_manager_apply_is_signature_gated(tmp_path: Path):
    """A second identical apply() must report no change (nothing to
    reload) — same pattern as the nftables blocked set / tc tree."""
    manager = dr.DnsRuleManager(conf_dir=str(tmp_path), reload_dnsmasq=False)
    devices = [_FakeDevice(1, "aa:bb:cc:dd:ee:01")]
    rules = [_FakeRule("global", None, "block", "ads.example.com")]
    assert manager.apply(devices, rules, [], {}) is True
    assert manager.apply(devices, rules, [], {}) is False  # unchanged
    rules2 = [_FakeRule("global", None, "block", "tracker.example.net")]
    assert manager.apply(devices, rules2, [], {}) is True  # content changed


# ---------------------------------------------------------------------------
# Presets registry sanity
# ---------------------------------------------------------------------------

def test_streaming_preset_has_no_urls_and_uses_curated_list():
    preset = dr.PRESETS["streaming"]
    assert preset.urls == []
    assert dr.fetch_preset(preset) == dr.STREAMING_DOMAINS


def test_porn_preset_has_no_urls_and_uses_curated_list():
    preset = dr.PRESETS["porn"]
    assert preset.urls == []
    assert dr.fetch_preset(preset) == dr.PORN_DOMAINS


def test_social_media_preset_lists_cyb3rko_sources():
    preset = dr.PRESETS["social-media"]
    assert any("cyb3rko/social-media-hosts-blocklists" in u for u in preset.urls)


# ---------------------------------------------------------------------------
# DB-layer CRUD (quota/db.py's domain_rules / dns_presets / dns_server cols)
# ---------------------------------------------------------------------------

# import asyncio  # noqa: E402  (grouped with the DB-test section, not the top)

from quota import db as _db  # noqa: E402


def _run(coro):
    return _get_loop().run_until_complete(coro)


def test_domain_rule_crud_roundtrip(tmp_path: Path):
    async def _body():
        database = _db.Database(tmp_path / "q.db")
        await database.connect()
        rule = await database.create_domain_rule(
            "global", "block", "ads.example.com")
        assert rule.id > 0
        assert rule.source == "manual"

        fetched = await database.get_domain_rule(rule.id)
        assert fetched is not None and fetched.domain == "ads.example.com"

        listed = await database.list_domain_rules()
        assert len(listed) == 1

        await database.update_domain_rule(rule.id, enabled=False)
        disabled = await database.get_domain_rule(rule.id)
        assert disabled.enabled is False

        await database.delete_domain_rule(rule.id)
        assert await database.get_domain_rule(rule.id) is None
        await database.close()
    _run(_body())


def test_domain_rule_scoped_delete_on_device_removal(tmp_path: Path):
    async def _body():
        database = _db.Database(tmp_path / "q.db")
        await database.connect()
        dev = await database.upsert_device("aa:bb:cc:dd:ee:01", name="phone")
        await database.create_domain_rule(
            "device", "block", "tiktok.com", scope_id=dev.id)
        assert len(await database.list_domain_rules()) == 1
        await database.delete_device(dev.id)
        # The device-scoped rule is orphaned once its device (and dnsmasq
        # tag) is gone, so delete_device sweeps it too.
        assert await database.list_domain_rules() == []
        await database.close()
    _run(_body())


def test_device_and_user_dns_server_persist(tmp_path: Path):
    async def _body():
        database = _db.Database(tmp_path / "q.db")
        await database.connect()
        user = await database.create_user(name="kid")
        await database.update_user(user.id, dns_server="1.1.1.3")
        refreshed = await database.get_user(user.id)
        assert refreshed.dns_server == "1.1.1.3"

        dev = await database.upsert_device("aa:bb:cc:dd:ee:02", name="tablet",
                                           user_id=user.id)
        await database.update_device(dev.id, dns_server="9.9.9.9")
        refreshed_dev = await database.get_device(dev.id)
        assert refreshed_dev.dns_server == "9.9.9.9"
        await database.close()
    _run(_body())


def test_preset_state_roundtrip(tmp_path: Path):
    async def _body():
        database = _db.Database(tmp_path / "q.db")
        await database.connect()
        assert await database.get_preset_state("ads-tracking") is None
        await database.set_preset_state("ads-tracking", True, domain_count=1234)
        state = await database.get_preset_state("ads-tracking")
        assert state["enabled"] == 1
        assert state["domain_count"] == 1234
        await database.set_preset_state("ads-tracking", False, domain_count=0)
        state = await database.get_preset_state("ads-tracking")
        assert state["enabled"] == 0
        await database.close()
    _run(_body())


# ---------------------------------------------------------------------------
# API-layer validation (api/schemas.py) — the FIRST line of defense against
# config injection via target_ip / dns_server; quota/dns_rules.py's
# _is_safe_config_token tests above are the second (defense in depth).
# ---------------------------------------------------------------------------

def test_api_rejects_config_injection_in_target_ip():
    from pydantic import ValidationError

    from api.schemas import DomainRuleCreate

    with pytest.raises(ValidationError):
        DomainRuleCreate(scope="global", action="redirect",
                         domain="evil.example.com",
                         target_ip="1.1.1.1\nno-resolv")


def test_api_accepts_valid_target_ip():
    from api.schemas import DomainRuleCreate

    rule = DomainRuleCreate(scope="global", action="redirect",
                            domain="printer.local", target_ip="192.168.2.50")
    assert rule.target_ip == "192.168.2.50"


def test_api_rejects_config_injection_in_dns_server():
    from pydantic import ValidationError

    from api.schemas import DnsServerUpdate

    with pytest.raises(ValidationError):
        DnsServerUpdate(dns_server="1.1.1.1\nserver=evil")


def test_api_accepts_empty_dns_server_as_clear():
    from api.schemas import DnsServerUpdate

    assert DnsServerUpdate(dns_server="").dns_server == ""
    assert DnsServerUpdate(dns_server="9.9.9.9").dns_server == "9.9.9.9"


def test_render_tags_static_ipv6_reservation():
    """dhcp-host carries the optional IPv6 reservation in dnsmasq's [addr]
    syntax, for known devices and for standalone reservations."""
    from types import SimpleNamespace

    from quota.dns_rules import render_tags

    dev = SimpleNamespace(id=7, mac="aa:bb:cc:dd:ee:07")
    out = render_tags([dev], {
        "aa:bb:cc:dd:ee:07": ("192.168.2.50", "fd00:da7a:2::50"),
        "aa:bb:cc:dd:ee:09": {"ip": "192.168.2.51", "ip6": "fd00:da7a:2::51"},
        "aa:bb:cc:dd:ee:0a": "192.168.2.52",          # legacy mac -> ip form
    })
    assert "dhcp-host=aa:bb:cc:dd:ee:07,set:qmdev7,192.168.2.50,[fd00:da7a:2::50]" in out
    assert "dhcp-host=aa:bb:cc:dd:ee:09,192.168.2.51,[fd00:da7a:2::51]" in out
    assert "dhcp-host=aa:bb:cc:dd:ee:0a,192.168.2.52\n" in out


def test_manager_restores_previous_files_when_dnsmasq_rejects_them(tmp_path: Path, monkeypatch):
    """A file dnsmasq rejects must not stay in /etc/dnsmasq.d: the next
    dnsmasq restart would fail and take DHCP + DNS down (issue #41)."""
    manager = dr.DnsRuleManager(conf_dir=str(tmp_path), reload_dnsmasq=True)
    good = [_FakeRule("global", None, "block", "ads.example.com")]
    monkeypatch.setattr(manager, "_reload", lambda: True)
    assert manager.apply([], good, [], {}) is True
    before = (tmp_path / "quota-domains.conf").read_text()

    monkeypatch.setattr(manager, "_reload", lambda: False)
    bad = [_FakeRule("global", None, "block", "tracker.example.net")]
    assert manager.apply([_FakeDevice(1, "aa:bb:cc:dd:ee:01")], bad, [], {}) is False
    assert (tmp_path / "quota-domains.conf").read_text() == before
    assert "qmdev1" not in (tmp_path / "quota-tags.conf").read_text()


def test_manager_removes_new_file_when_dnsmasq_rejects_it(tmp_path: Path, monkeypatch):
    manager = dr.DnsRuleManager(conf_dir=str(tmp_path), reload_dnsmasq=True)
    monkeypatch.setattr(manager, "_reload", lambda: False)
    rules = [_FakeRule("global", None, "block", "ads.example.com")]
    assert manager.apply([], rules, [], {}) is False
    assert not (tmp_path / "quota-domains.conf").exists()
    assert not (tmp_path / "quota-tags.conf").exists()


def test_reload_reports_failed_validation(monkeypatch):
    manager = dr.DnsRuleManager()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "dnsmasq: bad option at line 2")

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    assert manager._reload() is False
    assert calls == [["dnsmasq", "--test"]]  # never restarted
