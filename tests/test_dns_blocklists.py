"""URL-fed DNS blocklist subscription tests."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from quota import db as _db

_cached_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _cached_loop
    if _cached_loop is None or _cached_loop.is_closed():
        _cached_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_cached_loop)
    return _cached_loop


def _run(coro):
    return _get_loop().run_until_complete(coro)


def test_blocklist_memberships_are_source_owned(tmp_path):
    async def body():
        db = _db.Database(tmp_path / "lists.db")
        await db.connect()
        first = await db.create_dns_blocklist(
            "Feed A", "https://lists.example/a", ["ads.example", "shared.example"])
        second = await db.create_dns_blocklist(
            "Feed B", "https://lists.example/b", ["shared.example"])

        assert await db.list_enabled_dns_blocklist_domains() == [
            "ads.example", "shared.example"]
        assert await db.set_dns_blocklist_enabled(second["id"], False) is False
        assert await db.list_enabled_dns_blocklist_domains() == [
            "ads.example", "shared.example"]
        assert await db.set_dns_blocklist_enabled(second["id"], True) is False

        assert await db.delete_dns_blocklist(first["id"]) is True
        assert await db.list_enabled_dns_blocklist_domains() == ["shared.example"]
        assert await db.get_dns_blocklist(second["id"]) is not None
        await db.close()

    _run(body())


def test_page_domain_rules_is_bounded_and_filters_presets(tmp_path):
    async def body():
        db = _db.Database(tmp_path / "pages.db")
        await db.connect()
        await db.create_domain_rules_bulk(
            "global", "block", [f"rule-{i}.example" for i in range(55)])
        await db.create_domain_rule(
            "global", "block", "preset-only.example", source="preset:ads")

        page = await db.page_domain_rules(limit=50, offset=50)
        assert len(page["items"]) == 5
        assert page["total"] == 55
        assert page["items"][0].domain == "rule-50.example"

        all_rows = await db.page_domain_rules(limit=500, offset=0)
        assert len(all_rows["items"]) == 55
        assert all_rows["limit"] == 100

        filtered = await db.page_domain_rules(limit=50, offset=0, query="RULE-54")
        assert [r.domain for r in filtered["items"]] == ["rule-54.example"]
        assert filtered["total"] == 1
        await db.close()

    _run(body())


def test_enabled_blocklist_rule_lookup_expands_observed_domain_suffixes(tmp_path):
    async def body():
        db = _db.Database(tmp_path / "blocklist-suffixes.db")
        await db.connect()
        await db.create_dns_blocklist(
            "Ads", "https://lists.example/ads",
            ["ads.example", "tracker.example.net"], enabled=True)
        await db.create_dns_blocklist(
            "Disabled", "https://lists.example/disabled",
            ["child.ads.example"], enabled=False)

        rules = await db.list_enabled_dns_blocklist_rules_for_domains(
            {"video.ads.example", "img.tracker.example.net", "other.example.org"})
        assert {rule.domain for rule in rules} == {"ads.example", "tracker.example.net"}
        assert all(rule.source == "blocklist" for rule in rules)
        await db.close()

    _run(body())


def test_validate_blocklist_url_rejects_private_destination(monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(
        dns_blocklists, "_resolve_addresses", lambda _host: {"127.0.0.1"})
    with pytest.raises(dns_blocklists.BlocklistError) as exc:
        dns_blocklists.validate_blocklist_url("https://lists.example/feed.txt")
    assert exc.value.code == "unsafe_destination"


@pytest.mark.parametrize("url", [
    "http://lists.example/feed.txt",
    "https://user:pass@lists.example/feed.txt",
    "https://localhost/feed.txt",
    "https://lists.example:0/feed.txt",
])
def test_validate_blocklist_url_rejects_unsafe_url_shapes(url):
    from quota import dns_blocklists

    with pytest.raises(dns_blocklists.BlocklistError):
        dns_blocklists.validate_blocklist_url(url)


def test_fetch_blocklist_domains_parses_hosts_and_adblock(monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(
        dns_blocklists, "_resolve_addresses", lambda _host: {"8.8.8.8"})
    bodies = iter([
        b"0.0.0.0 ads.example\n127.0.0.1 tracker.example\n",
        b"! title\n||ads.example^\n||video.example^$third-party\n",
    ])
    monkeypatch.setattr(
        dns_blocklists, "_fetch_response",
        lambda _url, _ip: (200, {}, next(bodies)))

    assert dns_blocklists.fetch_blocklist_domains(
        "https://lists.example/hosts") == {"ads.example", "tracker.example"}
    assert dns_blocklists.fetch_blocklist_domains(
        "https://lists.example/abp") == {"ads.example", "video.example"}


def test_fetch_blocklist_domains_rejects_html_and_empty_feed(monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(
        dns_blocklists, "_resolve_addresses", lambda _host: {"8.8.8.8"})
    bodies = iter([b"<!doctype html><title>error</title>", b"# empty list\n"])
    monkeypatch.setattr(
        dns_blocklists, "_fetch_response",
        lambda _url, _ip: (200, {}, next(bodies)))
    for code in ("unsupported_feed", "empty_feed"):
        with pytest.raises(dns_blocklists.BlocklistError) as exc:
            dns_blocklists.fetch_blocklist_domains("https://lists.example/list")
        assert exc.value.code == code


def test_fetch_blocklist_domains_validates_redirect_destination(monkeypatch):
    from quota import dns_blocklists

    def resolve(host):
        return {"8.8.8.8" if host == "lists.example" else "10.0.0.2"}

    monkeypatch.setattr(dns_blocklists, "_resolve_addresses", resolve)
    monkeypatch.setattr(
        dns_blocklists, "_fetch_response",
        lambda _url, _ip: (302, {"location": "https://private.example/list"}, b""))
    with pytest.raises(dns_blocklists.BlocklistError) as exc:
        dns_blocklists.fetch_blocklist_domains("https://lists.example/list")
    assert exc.value.code == "unsafe_destination"


def test_fetch_blocklist_domains_enforces_redirect_and_body_limits(monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(
        dns_blocklists, "_resolve_addresses", lambda _host: {"8.8.8.8"})
    monkeypatch.setattr(dns_blocklists, "MAX_REDIRECTS", 1)
    monkeypatch.setattr(
        dns_blocklists, "_fetch_response",
        lambda _url, _ip: (302, {"location": "/again"}, b""))
    with pytest.raises(dns_blocklists.BlocklistError) as exc:
        dns_blocklists.fetch_blocklist_domains("https://lists.example/list")
    assert exc.value.code == "too_many_redirects"

    monkeypatch.setattr(dns_blocklists, "MAX_RESPONSE_BYTES", 4)
    monkeypatch.setattr(
        dns_blocklists, "_fetch_response",
        lambda _url, _ip: (200, {}, b"12345"))
    with pytest.raises(dns_blocklists.BlocklistError) as exc:
        dns_blocklists.fetch_blocklist_domains("https://lists.example/list")
    assert exc.value.code == "response_too_large"


def test_blocklist_manager_failed_refresh_keeps_last_good_domains(
        tmp_path, monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(dns_blocklists, "validate_blocklist_url", lambda url: url)
    async def body():
        db = _db.Database(tmp_path / "refresh.db")
        await db.connect()
        manager = dns_blocklists.DnsBlocklistManager(
            db, fetcher=lambda _url: {"ads.example"})
        item, changed = await manager.add("Feed", "https://lists.example/a")
        assert changed is True
        assert item["enabled"] == 1

        def fail(_url):
            raise dns_blocklists.BlocklistError("fetch_failed")

        manager.fetcher = fail
        with pytest.raises(dns_blocklists.BlocklistError) as exc:
            await manager.refresh(item["id"])
        assert exc.value.code == "fetch_failed"
        current = await db.get_dns_blocklist(item["id"])
        assert current["last_error"] == "fetch_failed"
        assert await db.list_enabled_dns_blocklist_domains() == ["ads.example"]
        await db.close()

    _run(body())


def test_blocklist_manager_defaults_name_to_source_hostname(tmp_path, monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(dns_blocklists, "validate_blocklist_url", lambda url: url)

    async def body():
        db = _db.Database(tmp_path / "default-name.db")
        await db.connect()
        manager = dns_blocklists.DnsBlocklistManager(
            db, fetcher=lambda _url: {"ads.example"})
        item, _changed = await manager.add(
            "", "https://lists.example/feeds/ads.txt")
        assert item["name"] == "lists.example"
        await db.close()

    _run(body())


def test_blocklist_manager_capacity_failure_is_atomic(tmp_path, monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(dns_blocklists, "validate_blocklist_url", lambda url: url)
    monkeypatch.setattr(dns_blocklists, "MAX_ACTIVE_DOMAINS", 2)
    async def body():
        db = _db.Database(tmp_path / "capacity.db")
        await db.connect()
        domains = {"one.example"}
        manager = dns_blocklists.DnsBlocklistManager(
            db, fetcher=lambda _url: set(domains))
        first, _ = await manager.add("First", "https://lists.example/first")
        domains.update({"two.example"})
        await manager.add("Second", "https://lists.example/second")
        domains.clear()
        domains.update({"new-a.example", "new-b.example"})

        with pytest.raises(dns_blocklists.BlocklistError) as exc:
            await manager.refresh(first["id"])
        assert exc.value.code == "capacity_exceeded"
        assert await db.list_enabled_dns_blocklist_domains() == [
            "one.example", "two.example"]
        await db.close()

    _run(body())


def test_blocklist_manager_does_not_retry_until_interval_after_failure(
        tmp_path, monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(dns_blocklists, "validate_blocklist_url", lambda url: url)
    async def body():
        db = _db.Database(tmp_path / "due.db")
        await db.connect()
        manager = dns_blocklists.DnsBlocklistManager(
            db, fetcher=lambda _url: {"ads.example"})
        item, _ = await manager.add("Feed", "https://lists.example/a")
        attempted = item["last_attempt_at"]
        manager.fetcher = lambda _url: (_ for _ in ()).throw(
            dns_blocklists.BlocklistError("fetch_failed"))
        with pytest.raises(dns_blocklists.BlocklistError):
            await manager.refresh(item["id"])
        failed_state = await db.get_dns_blocklist(item["id"])
        attempted = failed_state["last_attempt_at"]

        fetch_calls = 0

        def count_failure(_url):
            nonlocal fetch_calls
            fetch_calls += 1
            raise dns_blocklists.BlocklistError("fetch_failed")

        manager.fetcher = count_failure
        assert await manager.refresh_due(
            now=attempted + dns_blocklists.REFRESH_INTERVAL_SECONDS - 1) is False
        assert fetch_calls == 0
        assert await manager.refresh_due(
            now=attempted + dns_blocklists.REFRESH_INTERVAL_SECONDS + 1) is False
        assert fetch_calls == 1
        await db.close()

    _run(body())


def test_blocklist_manager_limits_concurrent_fetches_to_two(tmp_path, monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(dns_blocklists, "validate_blocklist_url", lambda url: url)

    async def body():
        db = _db.Database(tmp_path / "concurrency.db")
        await db.connect()
        manager = dns_blocklists.DnsBlocklistManager(
            db, fetcher=lambda url: {url.rsplit("/", 1)[-1] + ".example"})
        for index in range(4):
            await manager.add(f"Feed {index}", f"https://lists.example/{index}")

        gate = threading.Event()
        active_lock = threading.Lock()
        active = 0
        maximum = 0

        def slow_fetch(url):
            nonlocal active, maximum
            with active_lock:
                active += 1
                maximum = max(maximum, active)
            gate.wait(timeout=2)
            with active_lock:
                active -= 1
            return {url.rsplit("/", 1)[-1] + ".example"}

        manager.fetcher = slow_fetch
        due_task = asyncio.create_task(manager.refresh_due(
            now=time.time() + dns_blocklists.REFRESH_INTERVAL_SECONDS + 1))
        for _ in range(100):
            await asyncio.sleep(0.01)
            with active_lock:
                if maximum == 2:
                    break
        with active_lock:
            assert maximum == 2
        gate.set()
        assert await due_task is False
        with active_lock:
            assert maximum == 2
        await db.close()

    _run(body())


def test_blocklist_manager_serializes_refreshes_for_one_feed(tmp_path, monkeypatch):
    from quota import dns_blocklists

    monkeypatch.setattr(dns_blocklists, "validate_blocklist_url", lambda url: url)

    async def body():
        db = _db.Database(tmp_path / "same-feed.db")
        await db.connect()
        manager = dns_blocklists.DnsBlocklistManager(
            db, fetcher=lambda _url: {"ads.example"})
        item, _ = await manager.add("Feed", "https://lists.example/feed")
        gate = threading.Event()
        active_lock = threading.Lock()
        active = 0
        maximum = 0

        def slow_fetch(_url):
            nonlocal active, maximum
            with active_lock:
                active += 1
                maximum = max(maximum, active)
            gate.wait(timeout=2)
            with active_lock:
                active -= 1
            return {"ads.example"}

        manager.fetcher = slow_fetch
        first = asyncio.create_task(manager.refresh(item["id"]))
        for _ in range(100):
            await asyncio.sleep(0.01)
            with active_lock:
                if active:
                    break
        second = asyncio.create_task(manager.refresh(item["id"]))
        await asyncio.sleep(0.05)
        with active_lock:
            assert maximum == 1
            assert active == 1
        gate.set()
        await asyncio.gather(first, second)
        with active_lock:
            assert maximum == 1
        await db.close()

    _run(body())
