import asyncio
import time

from quota.db import Database


def test_usage_batch_receipt_makes_ambiguous_retry_idempotent(tmp_path):
    db = Database(tmp_path / "usage.db")
    async def run():
        await db.connect()
        device = await db.upsert_device("aa:bb:cc:dd:ee:fa", "retry")
        await db.add_usage_batch_once("stable-batch", [(device.id, "2026-11-23", 1000, 0)], free_mode=True)
        # Models a commit whose acknowledgement was lost: caller retries same ID.
        await db.add_usage_batch_once("stable-batch", [(device.id, "2026-11-23", 1000, 0)], free_mode=False)
        usage = await db.get_usage(device.id)
        cur = await db.conn.execute("SELECT up_bytes FROM free_usage WHERE device_id=?", (device.id,))
        free = await cur.fetchone()
        assert usage["up_bytes"] == 1000
        assert free["up_bytes"] == 1000
        await db.close()
    asyncio.run(run())


def test_usage_batch_retry_supports_in_memory_database():
    db = Database(":memory:")
    async def run():
        await db.connect()
        device = await db.upsert_device("aa:bb:cc:dd:ee:fd", "memory")
        await db.add_usage_batch_once("memory-batch", [(device.id, "2026-11-23", 7, 0)], free_mode=False)
        assert (await db.get_usage(device.id))["up_bytes"] == 7
        await db.close()
    asyncio.run(run())


def test_usage_batch_keeps_deleted_device_usage_for_existing_owner(tmp_path):
    db = Database(tmp_path / "usage-deleted.db")
    async def run():
        await db.connect()
        removed = await db.upsert_device("aa:bb:cc:dd:ee:fb", "removed")
        live = await db.upsert_device("aa:bb:cc:dd:ee:fc", "live")
        await db.delete_device(removed.id)
        rows = [(removed.id, time.strftime("%Y-%m-%d"), 700, 0),
                (live.id, time.strftime("%Y-%m-%d"), 300, 0)]
        await db.add_usage_batch_once("mixed-batch", rows, free_mode=False)
        await db.add_usage_batch_once("mixed-batch", rows, free_mode=False)
        assert (await db.get_usage(removed.id))["up_bytes"] == 700
        assert (await db.get_usage(live.id))["up_bytes"] == 300
        by_user = await db.get_period_usage_by_user()
        assert by_user[removed.user_id]["up"] == 700
        cur = await db.conn.execute("SELECT up_bytes FROM free_usage WHERE device_id=?", (live.id,))
        assert await cur.fetchone() is None
        await db.close()
    asyncio.run(run())


def test_usage_batch_skips_unknown_and_purged_device_ids(tmp_path):
    db = Database(tmp_path / "usage-purged.db")
    async def run():
        await db.connect()
        user = await db.create_user("erased")
        device = await db.upsert_device("aa:bb:cc:dd:ee:fe", "erased", user_id=user.id)
        await db.delete_device(device.id)
        await db.delete_user(user.id)
        rows = [(device.id, time.strftime("%Y-%m-%d"), 700, 0),
                (999999, time.strftime("%Y-%m-%d"), 900, 0)]
        await db.add_usage_batch_once("purged-batch", rows, free_mode=True)
        assert (await db.get_usage(device.id))["up_bytes"] == 0
        assert await db.get_period_usage_by_user() == {}
        await db.close()
    asyncio.run(run())
