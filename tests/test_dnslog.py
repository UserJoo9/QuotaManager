"""Unit tests for the dnsmasq query-log tailer + DNS-history DB layer.

No root / no hardware: the parser is tested against captured dnsmasq line
shapes, the tailer against temp files, and the DB against a temp SQLite file.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
from pathlib import Path

import pytest

from quota import db as _db
from quota.dnslog import MINUTE_FMT, DnslogTailer, parse_dnslog_line


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def _fixed_now() -> _dt.datetime:
    return _dt.datetime(2026, 8, 10, 14, 30, tzinfo=_dt.timezone.utc)


def test_parse_dnslog_query_line():
    ev = parse_dnslog_line(
        "query[A] example.com from 192.168.2.100", _fixed_now)
    assert ev is not None
    assert ev.ip == "192.168.2.100"
    assert ev.domain == "example.com"
    assert ev.minute == _fixed_now().strftime(MINUTE_FMT)


def test_parse_dnslog_serial_extra_shape():
    # log-queries=extra with the syslog-style serial + dnsmasq[pid]: prefix
    ev = parse_dnslog_line(
        "Aug 10 14:30:01 dnsmasq[1234]: 42 query[A] www.example.com from "
        "192.168.2.101", _fixed_now)
    assert ev is not None
    assert ev.domain == "www.example.com"
    assert ev.ip == "192.168.2.101"


def test_parse_dnslog_extra_ip_port_shape():
    # log-queries=extra on dnsmasq >= 2.90 stamps the client ip/port between
    # the serial and query[type] — the exact shape captured on the gateway
    # box (regression: the parser previously returned None for every line).
    ev = parse_dnslog_line(
        "Aug 10 00:00:54 dnsmasq[862442]: 1 192.168.2.186/16773 query[A] "
        "icosa-sg.coloros.com from 192.168.2.186", _fixed_now)
    assert ev is not None
    assert ev.domain == "icosa-sg.coloros.com"
    assert ev.ip == "192.168.2.186"
    # an ip/port chunk with no leading serial is accepted too
    ev = parse_dnslog_line(
        "192.168.2.186/16773 query[AAAA] example.net from 192.168.2.186",
        _fixed_now)
    assert ev is not None
    assert ev.domain == "example.net"
    assert ev.ip == "192.168.2.186"


def test_parse_dnslog_strips_optional_prefixes():
    # syslog prefix with a serial but no [pid]
    ev = parse_dnslog_line("123 query[AAAA] example.net from 192.168.2.102",
                           _fixed_now)
    assert ev is not None
    assert ev.domain == "example.net"
    # bare query line (file-mode, no timestamp — what log-facility produces)
    ev = parse_dnslog_line("query[TXT] example.org from 192.168.2.103",
                           _fixed_now)
    assert ev is not None
    assert ev.domain == "example.org"


def test_parse_dnslog_lowercases_and_strips_trailing_dot():
    ev = parse_dnslog_line("query[A] Example.COM. from 192.168.2.100",
                           _fixed_now)
    assert ev is not None
    assert ev.domain == "example.com"


def test_parse_dnslog_ignores_non_query_lines():
    # forwarded / reply / cached / config / DHCP / overflow lines never match —
    # including the serial + ip/port prefix those lines carry under extra
    for line in (
        "forwarded www.example.com to 8.8.8.8",
        "reply www.example.com is 93.184.216.34",
        "1 192.168.2.186/16773 forwarded www.example.com to 8.8.8.8",
        "1 192.168.2.186/16773 reply www.example.com is 93.184.216.34",
        "2 192.168.2.186/61779 reply query is duplicate",
        "cached www.example.com is 93.184.216.34",
        "config 127.0.0.1 is 127.0.0.1",
        "DHCPACK(eth0) aa:bb:cc:dd:ee:ff 192.168.2.100 host",
        "dnsmasq: overflow: 5 queries at max",
    ):
        assert parse_dnslog_line(line, _fixed_now) is None, line


def test_parse_dnslog_filters_reverse_pointer_names():
    # PTR lookups are DNS housekeeping (IP -> name), not browsing
    for line in (
        "query[PTR] 100.2.168.192.in-addr.arpa from 192.168.2.1",
        "query[PTR] 4.4.0.0.ip6.arpa from 192.168.2.1",
    ):
        assert parse_dnslog_line(line, _fixed_now) is None, line


# ---------------------------------------------------------------------------
# tailer
# ---------------------------------------------------------------------------

def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def test_tailer_first_start_seeks_to_eof(tmp_path):
    """No resume state -> pre-feature lines are never attributed."""
    logf = tmp_path / "q.log"
    _write(logf, "query[A] old.example.com from 192.168.2.100\n")
    t = DnslogTailer(str(logf))
    try:
        t._read_pass()  # sync single pass; sets cursor at EOF, reads nothing
        assert t.drain_events() == []
    finally:
        t.stop()


def test_tailer_reads_new_lines_from_offset(tmp_path):
    logf = tmp_path / "q.log"
    _write(logf, "query[A] example.com from 192.168.2.100\n")
    t = DnslogTailer(str(logf))
    try:
        t._read_pass()
        assert t.drain_events() == []
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write("query[A] new.example.com from 192.168.2.100\n")
        t._read_pass()
        evs = t.drain_events()
        assert len(evs) == 1
        assert evs[0].domain == "new.example.com"
    finally:
        t.stop()


def test_tailer_resumes_from_persisted_state(tmp_path):
    logf = tmp_path / "q.log"
    _write(logf, "query[A] old.example.com from 192.168.2.100\n"
                 "query[A] new.example.com from 192.168.2.100\n")
    t = DnslogTailer(str(logf))
    try:
        t._read_pass()
        assert t.drain_events() == []
        resume = t.state_snapshot()
        assert resume["inode"] == os.stat(logf).st_ino
        # a second tailer resuming at the same offset reads only NEW bytes
        t2 = DnslogTailer(str(logf), resume=resume)
        try:
            t2._read_pass()
            assert t2.drain_events() == []
        finally:
            t2.stop()
    finally:
        t.stop()


def test_tailer_detects_truncation_resets_offset(tmp_path):
    logf = tmp_path / "q.log"
    logf.write_bytes(b"")  # start empty (tail semantics: pre-existing is skipped)
    t = DnslogTailer(str(logf))
    try:
        t._read_pass()
        # grow it, read a line, then copytruncate to a SHORTER file
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write("query[A] example.com from 192.168.2.100\n")
        t._read_pass()
        assert t.drain_events()[0].domain == "example.com"
        # copytruncate to a SHORTER file: offset 43 > new size, so the size
        # shrink (not an inode change) must reset the cursor to 0
        _write(logf, "query[A] t.com from 192.168.2.100\n")
        t._read_pass()
        evs = t.drain_events()
        assert len(evs) == 1 and evs[0].domain == "t.com"
    finally:
        t.stop()


def test_tailer_detects_rotation_new_inode(tmp_path):
    logf = tmp_path / "q.log"
    _write(logf, "query[A] example.com from 192.168.2.100\n")
    t = DnslogTailer(str(logf))
    try:
        t._read_pass()
        assert t.drain_events() == []
        # logrotate create/rename mode: a fresh file replaces the old one
        newf = tmp_path / "q.log.1"
        _write(newf, "query[A] rotated.com from 192.168.2.100\n")
        os.replace(newf, logf)
        t._read_pass()
        evs = t.drain_events()
        assert len(evs) == 1 and evs[0].domain == "rotated.com"
    finally:
        t.stop()


def test_tailer_handles_partial_line_at_eof(tmp_path):
    """A query line split across two reads still parses once it completes."""
    logf = tmp_path / "q.log"
    logf.write_bytes(b"")  # start empty (tail semantics)
    t = DnslogTailer(str(logf))
    try:
        t._read_pass()
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write("query[A] example.com from 192.168.2.100\n")
        t._read_pass()
        assert t.drain_events()[0].domain == "example.com"
        # half a query line lands in this read…
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write("query[A] ")
        t._read_pass()
        assert t.drain_events() == []  # partial line held, not emitted
        # …and the other half arrives in the next
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write("second.com from 192.168.2.100\n")
        t._read_pass()
        evs = t.drain_events()
        assert [e.domain for e in evs] == ["second.com"]
    finally:
        t.stop()


def test_tailer_skips_nul_sparse_hole(tmp_path):
    logf = tmp_path / "q.log"
    _write(logf, "query[A] before.com from 192.168.2.100\n")
    t = DnslogTailer(str(logf))
    try:
        t._read_pass()
        assert t.drain_events() == []
        # a copytruncate NUL hole then new data after it
        with open(logf, "ab") as fh:
            fh.write(b"\x00\x00\x00" + b"query[A] after.com from 192.168.2.100\n")
        t._read_pass()
        evs = t.drain_events()
        assert len(evs) == 1 and evs[0].domain == "after.com"
    finally:
        t.stop()


def test_tailer_tolerates_missing_log_file(tmp_path):
    """A missing query log must never kill the poll thread (the dnsmasq
    fragment may not be installed yet). A bare ``_read_pass`` raises OSError
    — the production ``_loop`` catches it and keeps polling."""
    t = DnslogTailer(str(tmp_path / "does-not-exist.log"))
    try:
        with pytest.raises(OSError):
            t._read_pass()
        assert t._inode is None, "no cursor is established for a missing file"
        assert t.drain_events() == []
        # the real invariant: the thread survives a missing file
        t.start()
        t._stop.wait(t.POLL_INTERVAL * 2.5)
        assert t.running is True
    finally:
        t.stop()


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

@pytest.fixture
def database(tmp_path):
    d = _db.Database(tmp_path / "dnslog.db")

    async def _connect():
        await d.connect()
        return d
    return _connect


_cached_loop = None
def _get_loop():
    global _cached_loop
    if _cached_loop is None or _cached_loop.is_closed():
        _cached_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_cached_loop)
    return _cached_loop

def run(coro):
    return _get_loop().run_until_complete(coro)


async def _seed(d, device_id, rows):
    await d.batch_add_dns_history(
        [(device_id, minute, domain, count)
         for minute, domain, count in rows])


def test_batch_add_dns_history_upserts_counts(database):
    d = run(database())
    run(_seed(d, 1, [("2026-08-10 14:00", "a.com", 2),
                     ("2026-08-10 14:01", "a.com", 3),
                     ("2026-08-10 14:00", "b.com", 1)]))
    # same (device, minute, domain) merges across calls
    run(_seed(d, 1, [("2026-08-10 14:00", "a.com", 5)]))
    hist = run(d.get_dns_history(1, "2026-08-10 00:00"))
    assert hist["total"] == 2 + 3 + 1 + 5
    assert next(t for t in hist["top_domains"]
                if t["domain"] == "a.com")["hits"] == 10


def test_get_dns_history_aggregates_top_activity_recent(database):
    d = run(database())
    run(_seed(d, 7, [("2026-08-10 14:00", "a.com", 4),
                     ("2026-08-10 14:00", "b.com", 1),
                     ("2026-08-10 14:01", "a.com", 2),
                     ("2026-08-10 14:02", "c.com", 3)]))
    hist = run(d.get_dns_history(7, "2026-08-10 14:00", limit=10))
    assert hist["total"] == 10
    assert [t["domain"] for t in hist["top_domains"]] == ["a.com", "c.com", "b.com"]
    assert [a["minute"] for a in hist["activity"]] == [
        "2026-08-10 14:00", "2026-08-10 14:01", "2026-08-10 14:02"]
    assert hist["recent"][0]["minute"] == "2026-08-10 14:02"
    # window filter drops older buckets
    hist24 = run(d.get_dns_history(7, "2026-08-10 14:01"))
    assert hist24["total"] == 5


def test_get_dns_history_all_devices_aggregates(database):
    """device_id=None sums domains/activity across every device and stamps
    each recent row with its owning device_id (the household "All devices"
    view; the frontend badges rows with [name])."""
    d = run(database())
    run(_seed(d, 1, [("2026-08-10 14:00", "a.com", 4),
                     ("2026-08-10 14:00", "b.com", 1),
                     ("2026-08-10 14:01", "a.com", 2)]))
    run(_seed(d, 2, [("2026-08-10 14:00", "a.com", 3),
                     ("2026-08-10 14:02", "c.com", 5)]))
    hist = run(d.get_dns_history(None, "2026-08-10 14:00", limit=10))
    # a.com spans both devices (4+2 + 3), b.com/c.com are single-device
    assert hist["total"] == 4 + 1 + 2 + 3 + 5
    assert [t["domain"] for t in hist["top_domains"]] == ["a.com", "c.com", "b.com"]
    assert [t["hits"] for t in hist["top_domains"]] == [9, 5, 1]
    assert [a["minute"] for a in hist["activity"]] == [
        "2026-08-10 14:00", "2026-08-10 14:01", "2026-08-10 14:02"]
    # newest-first, each row attributed to the device that made the query
    assert [r["device_id"] for r in hist["recent"]] == [2, 1, 1, 2, 1]
    assert hist["recent"][0]["domain"] == "c.com"
    # a device id still scopes to that device (no cross-device bleed)
    solo = run(d.get_dns_history(1, "2026-08-10 14:00", limit=10))
    assert solo["total"] == 7
    assert [t["domain"] for t in solo["top_domains"]] == ["a.com", "b.com"]


def test_prune_dns_history_scoped_per_user(database):
    d = run(database())
    # two users, each with one device
    uid1 = run(d.create_user("u1")).id
    uid2 = run(d.create_user("u2")).id
    dev1 = run(d.upsert_device("aa:bb:cc:dd:ee:01", user_id=uid1)).id
    dev2 = run(d.upsert_device("aa:bb:cc:dd:ee:02", user_id=uid2)).id
    run(_seed(d, dev1, [("2026-08-01 10:00", "old.com", 1),
                        ("2026-08-10 10:00", "new.com", 1)]))
    run(_seed(d, dev2, [("2026-08-01 10:00", "old.com", 1),
                        ("2026-08-10 10:00", "new.com", 1)]))
    # user1's 2-day cutoff deletes only THEIR old rows
    deleted = run(d.prune_dns_history(uid1, "2026-08-08 00:00"))
    assert deleted == 1
    hist1 = run(d.get_dns_history(dev1, "2026-07-01 00:00"))
    assert [t["domain"] for t in hist1["top_domains"]] == ["new.com"]
    hist2 = run(d.get_dns_history(dev2, "2026-07-01 00:00"))
    assert {t["domain"] for t in hist2["top_domains"]} == {"old.com", "new.com"}


def test_dns_history_device_delete_cleans_rows(database):
    d = run(database())
    uid = run(d.create_user("u")).id
    dev = run(d.upsert_device("aa:bb:cc:dd:ee:03", user_id=uid)).id
    run(_seed(d, dev, [("2026-08-10 14:00", "a.com", 1)]))
    run(d.delete_device(dev))
    hist = run(d.get_dns_history(dev, "2026-01-01 00:00"))
    assert hist["total"] == 0
