"""Database and service-boundary regression tests for guest vouchers."""

from __future__ import annotations

import asyncio
import re
import sqlite3

import pytest

from quota import db as _db
from quota.service import QuotaService


async def _pending_guest(database, mac: str):
    await database.set_setting("guest_mode", "1")
    device = await database.upsert_device(
        mac, "Visitor", _db.QUOTA_FIXED, fixed_gb=0.0, guest=True)
    await database.set_device_state(device.id, _db.BLOCK_GUEST_PENDING)
    await database.update_user(device.user_id,
                               block_state=_db.BLOCK_GUEST_PENDING)
    return device


def test_pending_guest_predicate_accepts_one_already_approved_side():
    from quota.guest_state import is_pending_guest

    assert is_pending_guest(True, _db.BLOCK_GUEST_PENDING, _db.BLOCK_OK)
    assert is_pending_guest(True, _db.BLOCK_OK, _db.BLOCK_GUEST_PENDING)
    assert not is_pending_guest(True, _db.BLOCK_ADMIN, _db.BLOCK_GUEST_PENDING)
    assert not is_pending_guest(False, _db.BLOCK_GUEST_PENDING,
                                _db.BLOCK_GUEST_PENDING)
    assert not is_pending_guest(True, "quota", _db.BLOCK_GUEST_PENDING)


def test_existing_guest_voucher_table_gets_revocation_column(tmp_path):
    """An installed DB created before vouchers supported revocation is upgraded."""
    path = tmp_path / "legacy-vouchers.db"
    with sqlite3.connect(path) as legacy:
        legacy.execute("""
            CREATE TABLE guest_vouchers (
                code TEXT PRIMARY KEY,
                quota_gb REAL NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL,
                used_at REAL,
                used_mac TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT ''
            )
        """)
        legacy.execute(
            "INSERT INTO guest_vouchers (code, quota_gb, created_at) "
            "VALUES ('KEEP-ME', 1.5, 1000)"
        )

    async def scenario():
        database = _db.Database(path)
        await database.connect()
        try:
            cursor = await database.conn.execute("PRAGMA table_info(guest_vouchers)")
            columns = {row["name"] for row in await cursor.fetchall()}
            assert "revoked_at" in columns
            assert {"used_user_id", "used_device_id",
                    "allowance_sync_pending"} <= columns
            voucher = await database.get_guest_voucher(" keep-me ")
            assert voucher is not None
            assert voucher.quota_gb == 1.5
            assert voucher.revoked_at is None
        finally:
            await database.close()

    asyncio.run(scenario())


def test_revoke_persists_and_used_vouchers_cannot_be_revoked(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "voucher-revoke.db")
        await database.connect()
        try:
            unused = await database.create_guest_voucher("  unused-123  ", 2.0)
            assert await database.revoke_guest_voucher(unused.code)

            revoked = await database.get_guest_voucher("UNUSED-123")
            assert revoked is not None and revoked.revoked_at is not None
            assert await database.revoke_guest_voucher(unused.code) is False
            assert await database.list_guest_vouchers(active_only=True) == []

            await database.create_guest_voucher("used-456", 1.0)
            device = await _pending_guest(
                database, "aa:bb:cc:dd:ee:01")
            ok, _, _ = await database.redeem_guest_voucher_for_device(
                "used-456", device.mac)
            assert ok
            assert await database.revoke_guest_voucher("used-456") is False
        finally:
            await database.close()

    asyncio.run(scenario())


def test_concurrent_redemption_grants_one_voucher_only(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "voucher-race.db")
        await database.connect()
        try:
            await database.create_guest_voucher("one-time-789", 3.25)
            devices = [
                await _pending_guest(database, "aa:bb:cc:dd:ee:01"),
                await _pending_guest(database, "aa:bb:cc:dd:ee:02"),
            ]
            results = await asyncio.gather(
                database.redeem_guest_voucher_for_device(
                    "ONE-TIME-789", devices[0].mac),
                database.redeem_guest_voucher_for_device(
                    " one-time-789 ", devices[1].mac),
            )
            assert sum(1 for success, _, _ in results if success) == 1
            assert sum(quota or 0.0 for _, _, quota in results) == 3.25
        finally:
            await database.close()

    asyncio.run(scenario())


def test_same_guest_retry_is_idempotent_and_does_not_regrant(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "voucher-idempotent.db")
        await database.connect()
        try:
            device = await _pending_guest(database, "aa:bb:cc:dd:ee:41")
            await database.create_guest_voucher("retry-5678", 3.0)

            first = await database.redeem_guest_voucher_for_device(
                "retry-5678", device.mac)
            await database.set_setting("guest_mode", "0")
            retry = await database.redeem_guest_voucher_for_device(
                "retry-5678", device.mac)

            assert first == (True, "redeemed", 3.0)
            assert retry == (True, "already_redeemed", 3.0)
            user = await database.get_user(device.user_id)
            current = await database.get_device(device_id=device.id)
            voucher = await database.get_guest_voucher("retry-5678")
            assert user is not None and user.fixed_gb == 3.0
            assert current is not None and current.fixed_gb == 3.0
            assert voucher is not None and voucher.allowance_sync_pending
        finally:
            await database.close()

    asyncio.run(scenario())


def test_same_voucher_retry_rejects_changed_deleted_and_shared_accounts(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "voucher-stale-retry.db")
        await database.connect()
        try:
            changed = await _pending_guest(database, "aa:bb:cc:dd:ee:42")
            await database.create_guest_voucher("changed-5678", 3.0)
            assert (await database.redeem_guest_voucher_for_device(
                "changed-5678", changed.mac))[0]
            await database.update_user(changed.user_id, fixed_gb=4.0)
            assert await database.redeem_guest_voucher_for_device(
                "changed-5678", changed.mac) == (False, "guest_not_pending", None)

            shared = await _pending_guest(database, "aa:bb:cc:dd:ee:43")
            await database.create_guest_voucher("shared-5678", 2.0)
            assert (await database.redeem_guest_voucher_for_device(
                "shared-5678", shared.mac))[0]
            await database.upsert_device("aa:bb:cc:dd:ee:44", "Sibling",
                                         user_id=shared.user_id)
            assert await database.redeem_guest_voucher_for_device(
                "shared-5678", shared.mac) == (False, "guest_account_shared", None)

            deleted = await _pending_guest(database, "aa:bb:cc:dd:ee:45")
            await database.create_guest_voucher("deleted-5678", 1.0)
            assert (await database.redeem_guest_voucher_for_device(
                "deleted-5678", deleted.mac))[0]
            await database.delete_user(deleted.user_id)
            assert await database.redeem_guest_voucher_for_device(
                "deleted-5678", deleted.mac) == (False, "voucher_invalid", None)

            denied = await _pending_guest(database, "aa:bb:cc:dd:ee:48")
            await database.create_guest_voucher("denied-5678", 1.0)
            assert (await database.redeem_guest_voucher_for_device(
                "denied-5678", denied.mac))[0]
            await database.add_mac_list("deny", [denied.mac])
            assert await database.redeem_guest_voucher_for_device(
                "denied-5678", denied.mac) == (False, "device_blocked", None)
        finally:
            await database.close()

    asyncio.run(scenario())


def test_allowance_recovery_after_restart_uses_current_policy_once(tmp_path):
    path = tmp_path / "voucher-restart.db"

    async def claim_and_restart():
        database = _db.Database(path)
        await database.connect()
        device = await _pending_guest(database, "aa:bb:cc:dd:ee:46")
        await database.create_guest_voucher("restart-5678", 2.5)
        assert await database.redeem_guest_voucher_for_device(
            "restart-5678", device.mac) == (True, "redeemed", 2.5)
        await database.update_user(device.user_id, fixed_gb=4.0)
        await database.close()
        return device.user_id

    user_id = asyncio.run(claim_and_restart())

    async def recover():
        database = _db.Database(path)
        await database.connect()
        try:
            service = QuotaService(database)
            assert await service.recover_guest_voucher_allowances() is True
            bundle = await database.get_bundle()
            assert bundle.allowances[user_id] == 4.0
            voucher = await database.get_guest_voucher("restart-5678")
            assert voucher is not None and not voucher.allowance_sync_pending
            assert await service.recover_guest_voucher_allowances() is False
            assert (await database.get_bundle()).allowances[user_id] == 4.0
        finally:
            await database.close()

    asyncio.run(recover())


def test_concurrent_allowance_refreshes_serialize_and_preserve_bundle_policy(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "voucher-refresh-order.db")
        await database.connect()
        try:
            service = QuotaService(database)
            original_compute = service.compute_allowances
            first_entered = asyncio.Event()
            release_first = asyncio.Event()
            active = 0
            max_active = 0

            async def delayed_compute():
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                try:
                    if not first_entered.is_set():
                        first_entered.set()
                        await release_first.wait()
                    return await original_compute()
                finally:
                    active -= 1

            service.compute_allowances = delayed_compute
            first = asyncio.create_task(service.recompute_allowances())
            await first_entered.wait()
            second = asyncio.create_task(service.recompute_allowances())
            await asyncio.sleep(0)
            bundle = await database.get_bundle()
            bundle.total_gb = 222.0
            bundle.period_start = "2026-11-01"
            await database.set_bundle(bundle)
            release_first.set()
            await asyncio.gather(first, second)

            saved = await database.get_bundle()
            assert max_active == 1
            assert saved.total_gb == 222.0
            assert saved.period_start == "2026-11-01"
        finally:
            await database.close()

    asyncio.run(scenario())


def test_database_failure_before_commit_leaves_voucher_unspent(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "voucher-precommit-error.db")
        await database.connect()
        try:
            device = await _pending_guest(database, "aa:bb:cc:dd:ee:47")
            await database.create_guest_voucher("abort-5678", 1.0)
            await database.conn.execute("""
                CREATE TRIGGER reject_guest_approval BEFORE UPDATE ON users
                WHEN OLD.id = NEW.id AND OLD.guest = 1
                BEGIN SELECT RAISE(ABORT, 'injected approval failure'); END
            """)
            await database.conn.commit()

            with pytest.raises(Exception, match="injected approval failure"):
                await database.redeem_guest_voucher_for_device(
                    "abort-5678", device.mac)

            voucher = await database.get_guest_voucher("abort-5678")
            current = await database.get_device(device_id=device.id)
            assert voucher is not None and voucher.used_at is None
            assert current is not None and current.block_state == _db.BLOCK_GUEST_PENDING
        finally:
            await database.close()

    asyncio.run(scenario())


def test_memory_database_rejects_redemption_with_clear_reason():
    async def scenario():
        database = _db.Database(":memory:")
        await database.connect()
        try:
            await database.create_guest_voucher("memory-1234", 1.0)
            device = await _pending_guest(
                database, "aa:bb:cc:dd:ee:30")

            result = await database.redeem_guest_voucher_for_device(
                "memory-1234", device.mac)

            assert result == (False, "voucher_storage_unsupported", None)
            voucher = await database.get_guest_voucher("memory-1234")
            assert voucher is not None and voucher.used_at is None
        finally:
            await database.close()

    asyncio.run(scenario())


def test_fresh_claim_still_respects_disabled_and_paused_guest_gates(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "voucher-gates.db")
        await database.connect()
        try:
            disabled = await _pending_guest(database, "aa:bb:cc:dd:ee:49")
            await database.create_guest_voucher("disabled-5678", 1.0)
            await database.set_setting("guest_mode", "0")
            assert await database.redeem_guest_voucher_for_device(
                "disabled-5678", disabled.mac) == (
                    False, "guest_mode_disabled", None)

            paused = await _pending_guest(database, "aa:bb:cc:dd:ee:50")
            await database.create_guest_voucher("paused-5678", 1.0)
            await database.set_setting("stop_new_connections", "1")
            assert await database.redeem_guest_voucher_for_device(
                "paused-5678", paused.mac) == (
                    False, "new_guests_paused", None)
            for code in ("disabled-5678", "paused-5678"):
                voucher = await database.get_guest_voucher(code)
                assert voucher is not None and voucher.used_at is None
        finally:
            await database.close()

    asyncio.run(scenario())


def test_expired_and_revoked_vouchers_do_not_redeem(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "voucher-ineligible.db")
        await database.connect()
        try:
            await database.create_guest_voucher("expired-123", 1.0,
                                                expires_at=1.0)
            expired_guest = await _pending_guest(
                database, "aa:bb:cc:dd:ee:03")
            expired = await database.redeem_guest_voucher_for_device(
                "expired-123", expired_guest.mac)
            assert expired[0] is False and expired[2] is None

            await database.create_guest_voucher("revoked-456", 1.0)
            assert await database.revoke_guest_voucher("revoked-456")
            revoked_guest = await _pending_guest(
                database, "aa:bb:cc:dd:ee:04")
            revoked = await database.redeem_guest_voucher_for_device(
                "revoked-456", revoked_guest.mac)
            assert revoked[0] is False and revoked[2] is None
        finally:
            await database.close()

    asyncio.run(scenario())


def test_deny_list_rejects_voucher_without_consuming_it(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "voucher-deny.db")
        await database.connect()
        try:
            device = await _pending_guest(database, "aa:bb:cc:dd:ee:25")
            await database.create_guest_voucher("deny-1234", 1.0)
            await database.add_mac_list("deny", [device.mac])

            result = await database.redeem_guest_voucher_for_device(
                "deny-1234", device.mac)

            assert result == (False, "device_blocked", None)
            voucher = await database.get_guest_voucher("deny-1234")
            assert voucher is not None and voucher.used_at is None
            current = await database.get_device(device_id=device.id)
            assert current.block_state == _db.BLOCK_GUEST_PENDING
        finally:
            await database.close()

    asyncio.run(scenario())


def test_voucher_code_generation_and_device_scoped_cooldown():
    from quota.guest_vouchers import VoucherAttemptLimiter, generate_voucher_code

    code = generate_voucher_code()
    assert re.fullmatch(r"[A-Z2-9]{4}(?:-[A-Z2-9]{4}){3}", code)
    assert generate_voucher_code() != code

    now = [1000.0]
    limiter = VoucherAttemptLimiter(clock=lambda: now[0])
    mac = "AA:BB:CC:DD:EE:10"
    assert limiter.retry_after(mac) == 0
    for _ in range(5):
        limiter.record_invalid(mac)
    assert limiter.retry_after(mac.lower()) == 900
    assert limiter.retry_after("aa:bb:cc:dd:ee:11") == 0

    now[0] += 901
    assert limiter.retry_after(mac) == 0


def test_voucher_attempt_window_is_rolling():
    from quota.guest_vouchers import VoucherAttemptLimiter

    now = [0.0]
    limiter = VoucherAttemptLimiter(clock=lambda: now[0])
    mac = "aa:bb:cc:dd:ee:12"
    for _ in range(4):
        limiter.record_invalid(mac)
    now[0] = 901.0
    limiter.record_invalid(mac)
    assert limiter.retry_after(mac) == 0


def test_same_device_redemptions_keep_one_lock_under_device_pressure():
    from quota.guest_vouchers import VoucherAttemptLimiter

    async def scenario():
        limiter = VoucherAttemptLimiter(max_devices=1)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()
        active_same_mac = 0
        maximum_same_mac = 0

        async def redeem(entered):
            nonlocal active_same_mac, maximum_same_mac
            async with limiter.serialize("aa:bb:cc:dd:ee:30"):
                active_same_mac += 1
                maximum_same_mac = max(maximum_same_mac, active_same_mac)
                entered.set()
                if entered is first_entered:
                    await release_first.wait()
                active_same_mac -= 1

        first = asyncio.create_task(redeem(first_entered))
        await first_entered.wait()
        same_lock = limiter._locks["aa:bb:cc:dd:ee:30"]
        second = asyncio.create_task(redeem(second_entered))
        await asyncio.sleep(0)

        async with limiter.serialize("aa:bb:cc:dd:ee:31"):
            pass
        assert limiter._locks["aa:bb:cc:dd:ee:30"] is same_lock
        assert not second_entered.is_set()

        release_first.set()
        await asyncio.gather(first, second)
        assert maximum_same_mac == 1
        assert "aa:bb:cc:dd:ee:30" not in limiter._locks

    asyncio.run(scenario())


def test_guest_pending_state_survives_quota_reconciliation(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "guest-pending.db")
        await database.connect()
        try:
            service = QuotaService(database, timezone="Africa/Cairo")
            await service.ensure_period()
            device = await database.upsert_device(
                "aa:bb:cc:dd:ee:20", "Visitor", _db.QUOTA_FIXED,
                fixed_gb=1.0, guest=True)
            await database.set_device_state(device.id, "guest_pending")
            await database.update_user(device.user_id,
                                       block_state="guest_pending")

            await service.evaluate_blocks()
            device = await database.get_device(device_id=device.id)
            user = await database.get_user(device.user_id)
            assert device.block_state == "guest_pending"
            assert user.block_state == "guest_pending"
            assert QuotaService.resolve_device_state(
                user, device, user_quota_blocked=False) == "guest_pending"
        finally:
            await database.close()

    asyncio.run(scenario())


def test_guest_voucher_claim_and_allowance_update_are_atomic(tmp_path):
    async def scenario():
        database = _db.Database(tmp_path / "guest-voucher-atomic.db")
        await database.connect()
        try:
            await database.set_setting("guest_mode", "1")
            await database.create_guest_voucher("claim-1234", 2.0)
            devices = []
            for suffix in (21, 22):
                mac = f"aa:bb:cc:dd:ee:{suffix:02x}"
                dev = await database.upsert_device(
                    mac, "Visitor", _db.QUOTA_FIXED, fixed_gb=0.0,
                    guest=True)
                await database.set_device_state(dev.id, "guest_pending")
                await database.update_user(dev.user_id,
                                           block_state="guest_pending")
                devices.append(dev)

            results = await asyncio.gather(*[
                database.redeem_guest_voucher_for_device("claim-1234", d.mac)
                for d in devices
            ])
            assert sum(1 for success, _, _ in results if success) == 1
            assert sum(quota or 0.0 for _, _, quota in results) == 2.0

            saved = [await database.get_device(device_id=d.id) for d in devices]
            assert sum(d.fixed_gb == 2.0 for d in saved) == 1
            assert sum(d.block_state == _db.BLOCK_OK for d in saved) == 1
            assert sum(d.block_state == "guest_pending" for d in saved) == 1
        finally:
            await database.close()

    asyncio.run(scenario())
