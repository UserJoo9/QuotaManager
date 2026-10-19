"""Tests for Local Share (manager and REST API)."""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from quota import db as _db
from quota.engine import SnapshotHolder
from quota.local_share import LocalShareManager, sanitize_filename
from quota.service import QuotaService

TZ = ZoneInfo("Africa/Cairo")


def test_sanitize_filename():
    assert sanitize_filename("test.txt") == "test.txt"
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename('weird/name*?<>.png') == "name____.png"
    assert sanitize_filename("") == "shared_file"


def test_local_share_manager_crud(tmp_path):
    async def _test():
        mgr = LocalShareManager(tmp_path / "share_data")

        # 1. Save file
        content = b"Hello from local share!"
        stream = io.BytesIO(content)

        sf = await mgr.save_file_stream(
            stream=stream,
            filename="hello.txt",
            content_type="text/plain",
            sender_ip="192.168.1.50",
            ttl_seconds=3600,
        )
        assert sf.name == "hello.txt"
        assert sf.size == len(content)
        assert sf.sender_ip == "192.168.1.50"

        # 2. Get file
        res = await mgr.get_file(sf.id)
        assert res is not None
        path, fname, mime = res
        assert fname == "hello.txt"
        assert path.read_bytes() == content

        # 3. List
        data = await mgr.list_all()
        assert data["total_files"] == 1
        assert data["total_size"] == len(content)
        assert len(data["files"]) == 1
        assert data["files"][0]["id"] == sf.id

        # 4. Save text
        st = await mgr.save_text("Check this link: https://example.com", "192.168.1.60")
        assert st.text == "Check this link: https://example.com"
        data = await mgr.list_all()
        assert len(data["texts"]) == 1
        assert data["texts"][0]["id"] == st.id

        # 5. Delete text
        assert await mgr.delete_text(st.id) is True
        data = await mgr.list_all()
        assert len(data["texts"]) == 0

        # 6. Delete file
        assert await mgr.delete_file(sf.id) is True
        assert await mgr.get_file(sf.id) is None
        data = await mgr.list_all()
        assert data["total_files"] == 0

    asyncio.run(_test())


def test_local_share_manager_expiration(tmp_path):
    async def _test():
        mgr = LocalShareManager(tmp_path / "share_expire")
        stream = io.BytesIO(b"Ephemeral data")

        # Save with 1 second TTL
        sf = await mgr.save_file_stream(
            stream=stream,
            filename="temp.bin",
            content_type="application/octet-stream",
            sender_ip="192.168.1.10",
            ttl_seconds=1,
        )
        # Manually expire
        sf.expires_at = time.time() - 10
        mgr.files[sf.id] = sf

        # cleanup
        count = await mgr.cleanup_expired()
        assert count == 1
        assert sf.id not in mgr.files
        assert await mgr.get_file(sf.id) is None

    asyncio.run(_test())


@pytest.fixture
def share_client(tmp_path):
    database = _db.Database(tmp_path / "api.db")
    service = QuotaService(database, timezone="Africa/Cairo",
                           clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=TZ))
    holder = SnapshotHolder()
    share_mgr = LocalShareManager(tmp_path / "share_storage")

    async def _init():
        await database.connect()

    asyncio.run(_init())

    app = create_app(
        database=database,
        service=service,
        holder=holder,
        local_share_manager=share_mgr,
    )

    with TestClient(app, client=("192.168.1.100", 50000)) as c:
        yield c, share_mgr


def test_share_page_html(share_client):
    client, _ = share_client
    r = client.get("/share")
    assert r.status_code == 200
    assert "Local Share" in r.text


def test_share_api_full_flow(share_client):
    client, share_mgr = share_client

    # 1. List initially empty
    r = client.get("/api/share/list")
    assert r.status_code == 200
    data = r.json()
    assert data["files"] == []
    assert data["texts"] == []

    # 2. Upload file via raw stream
    test_bytes = b"Router LAN super high speed transfer test"
    headers = {
        "X-Filename": "test_doc.txt",
        "Content-Type": "text/plain",
        "X-QM-CSRF": "1",
    }
    r = client.post("/api/share/upload", content=test_bytes, headers=headers)
    assert r.status_code == 200
    up = r.json()
    assert up["ok"] is True
    file_id = up["file"]["id"]
    assert up["file"]["name"] == "test_doc.txt"
    assert up["file"]["size"] == len(test_bytes)

    # 3. Download file
    r = client.get(f"/api/share/download/{file_id}")
    assert r.status_code == 200
    assert r.content == test_bytes
    assert "test_doc.txt" in r.headers.get("content-disposition", "")

    # 4. Post text note
    r = client.post("/api/share/text", json={"text": "WiFi password: HomeNetwork123"},
                    headers={"X-QM-CSRF": "1"})
    assert r.status_code == 200
    text_item = r.json()["item"]
    text_id = text_item["id"]

    # 5. List items
    r = client.get("/api/share/list")
    data = r.json()
    assert len(data["files"]) == 1
    assert len(data["texts"]) == 1
    assert data["texts"][0]["text"] == "WiFi password: HomeNetwork123"

    # 6. Delete text
    r = client.delete(f"/api/share/texts/{text_id}", headers={"X-QM-CSRF": "1"})
    assert r.status_code == 200

    # 7. Delete file
    r = client.delete(f"/api/share/files/{file_id}", headers={"X-QM-CSRF": "1"})
    assert r.status_code == 200

    # 8. List empty again
    r = client.get("/api/share/list")
    data = r.json()
    assert len(data["files"]) == 0
    assert len(data["texts"]) == 0


def test_share_wan_unauthenticated_blocked(tmp_path):
    """Clients originating from a public WAN IP without admin login cookie must be blocked (403)."""
    database = _db.Database(tmp_path / "api_wan.db")
    service = QuotaService(database, timezone="Africa/Cairo",
                           clock=lambda: datetime(2026, 8, 15, 12, 0, tzinfo=TZ))
    holder = SnapshotHolder()
    share_mgr = LocalShareManager(tmp_path / "share_storage_wan")

    async def _init():
        await database.connect()

    asyncio.run(_init())

    app = create_app(
        database=database,
        service=service,
        holder=holder,
        local_share_manager=share_mgr,
    )

    # Test client simulating external WAN IP (e.g., 8.8.8.8)
    with TestClient(app, client=("8.8.8.8", 50000)) as c:
        r = c.get("/share")
        assert r.status_code == 403
        assert "Local Share is only accessible from the home network" in r.text

        r = c.get("/api/share/list")
        assert r.status_code == 403


def test_local_share_manager_folder_and_batch_unit(tmp_path):
    """Test folder archiving and batch bundling on LocalShareManager directly."""
    async def _test():
        mgr = LocalShareManager(tmp_path / "share_unit")

        # 1. Folder upload
        folder_id = await mgr.init_folder_upload(
            folder_name="MyPhotos",
            sender_ip="192.168.1.15",
            ttl_seconds=3600,
        )
        assert folder_id in mgr._folder_sessions

        # Append two files
        b1 = b"Photo 1 content"
        b2 = b"Photo 2 content in subfolder"
        await mgr.append_folder_file(folder_id, "photo1.jpg", io.BytesIO(b1))
        await mgr.append_folder_file(folder_id, "sub/photo2.jpg", io.BytesIO(b2))

        # Finalize folder
        sf = await mgr.finalize_folder_upload(folder_id)
        assert sf.id == folder_id
        assert sf.name == "MyPhotos.zip"
        assert sf.content_type == "application/zip"
        assert sf.size > 0

        # Verify ZIP contents
        zip_path = tmp_path / "share_unit" / folder_id / "MyPhotos.zip"
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path, "r") as zf:
            namelist = zf.namelist()
            assert "photo1.jpg" in namelist
            assert "sub/photo2.jpg" in namelist
            assert zf.read("photo1.jpg") == b1
            assert zf.read("sub/photo2.jpg") == b2

        # 2. Batch ZIP creation
        # Add another standalone file
        sf2 = await mgr.save_file_stream(
            stream=io.BytesIO(b"Notes"),
            filename="notes.txt",
            content_type="text/plain",
            sender_ip="192.168.1.20",
        )

        res = await mgr.create_batch_zip([sf.id, sf2.id])
        assert res is not None
        batch_path, download_name = res
        assert batch_path.exists()
        assert download_name == "LocalShare_Bundle.zip"
        with zipfile.ZipFile(batch_path, "r") as bz:
            b_names = bz.namelist()
            assert "MyPhotos.zip" in b_names
            assert "notes.txt" in b_names

        # 3. Batch deletion
        deleted_count = await mgr.delete_batch([sf.id, sf2.id])
        assert deleted_count == 2
        assert sf.id not in mgr.files
        assert sf2.id not in mgr.files

    asyncio.run(_test())


def test_local_share_folder_upload_api(share_client):
    """Test full HTTP REST API flow for directory upload and ZIP packaging."""
    client, share_mgr = share_client

    # 1. Initialize folder upload
    r = client.post(
        "/api/share/folder/init",
        json={"folder_name": "VacationDocs", "ttl": 3600},
        headers={"X-QM-CSRF": "1"},
    )
    assert r.status_code == 200
    init_data = r.json()
    assert init_data["ok"] is True
    folder_id = init_data["folder_id"]

    # 2. Stream individual files into the folder ZIP
    f1_bytes = b"Itinerary details"
    r = client.post(
        f"/api/share/folder/file?folder_id={folder_id}&path=itinerary.txt",
        content=f1_bytes,
        headers={"Content-Type": "text/plain", "X-QM-CSRF": "1"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["bytes"] == len(f1_bytes)

    f2_bytes = b"Hotel confirmation ticket"
    r = client.post(
        f"/api/share/folder/file?folder_id={folder_id}&path=tickets/hotel.pdf",
        content=f2_bytes,
        headers={"Content-Type": "application/pdf", "X-QM-CSRF": "1"},
    )
    assert r.status_code == 200

    # 3. Finalize folder upload
    r = client.post(
        "/api/share/folder/finalize",
        json={"folder_id": folder_id},
        headers={"X-QM-CSRF": "1"},
    )
    assert r.status_code == 200
    fin_data = r.json()
    assert fin_data["ok"] is True
    assert fin_data["file"]["name"] == "VacationDocs.zip"

    # 4. Download finalized ZIP from regular download endpoint
    r = client.get(f"/api/share/download/{folder_id}")
    assert r.status_code == 200
    assert "VacationDocs.zip" in r.headers.get("content-disposition", "")
    with zipfile.ZipFile(io.BytesIO(r.content), "r") as zf:
        assert "itinerary.txt" in zf.namelist()
        assert "tickets/hotel.pdf" in zf.namelist()
        assert zf.read("itinerary.txt") == f1_bytes


def test_local_share_batch_actions_api(share_client):
    """Test downloading multiple selected files as a ZIP bundle and batch delete."""
    client, share_mgr = share_client

    # Upload 2 files
    r1 = client.post(
        "/api/share/upload",
        content=b"File A content",
        headers={"X-Filename": "docA.txt", "Content-Type": "text/plain", "X-QM-CSRF": "1"},
    )
    assert r1.status_code == 200
    id1 = r1.json()["file"]["id"]

    r2 = client.post(
        "/api/share/upload",
        content=b"File B content",
        headers={"X-Filename": "docB.txt", "Content-Type": "text/plain", "X-QM-CSRF": "1"},
    )
    assert r2.status_code == 200
    id2 = r2.json()["file"]["id"]

    # Download batch ZIP
    r_zip = client.get(f"/api/share/download-zip?ids={id1},{id2}")
    assert r_zip.status_code == 200
    assert "LocalShare_Bundle.zip" in r_zip.headers.get("content-disposition", "")
    with zipfile.ZipFile(io.BytesIO(r_zip.content), "r") as zf:
        namelist = zf.namelist()
        assert "docA.txt" in namelist
        assert "docB.txt" in namelist
        assert zf.read("docA.txt") == b"File A content"
        assert zf.read("docB.txt") == b"File B content"

    # Batch delete
    r_del = client.post(
        "/api/share/delete-batch",
        json={"ids": [id1, id2]},
        headers={"X-QM-CSRF": "1"},
    )
    assert r_del.status_code == 200
    assert r_del.json()["deleted"] == 2

    # Verify list is empty
    r_list = client.get("/api/share/list")
    assert len(r_list.json()["files"]) == 0


def test_live_share_hub_unit(tmp_path):
    """Unit test LiveShareHub message routing and binary chunk forwarding."""
    async def _test():
        hub = LocalShareManager(tmp_path / "hub_test").live_hub

        class FakeWS:
            def __init__(self):
                self.sent_json = []
                self.sent_bytes = []
            async def send_json(self, msg):
                self.sent_json.append(msg)
            async def send_bytes(self, b):
                self.sent_bytes.append(b)

        ws1 = FakeWS()
        ws2 = FakeWS()
        await hub.register_peer("p1", "192.168.1.10", "Phone", ws1)
        await hub.register_peer("p2", "192.168.1.20", "Laptop", ws2)

        assert len(hub.peers) == 2
        peers = hub.get_peer_list()
        assert len(peers) == 2

        # Register transfer
        await hub.register_transfer("transfer_123", "p1", "p2")
        assert "transfer_123" in hub.active_transfers

        # Route binary chunk
        header = "transfer_123".encode("ascii").ljust(16, b" ")
        payload = b"Hello direct stream!"
        chunk = header + payload

        routed = await hub.route_binary_chunk(chunk)
        assert routed is True
        assert len(ws2.sent_bytes) == 1
        assert ws2.sent_bytes[0] == chunk

        # Unregister transfer
        await hub.unregister_transfer("transfer_123")
        assert "transfer_123" not in hub.active_transfers

        # Unregister peer
        await hub.unregister_peer("p1")
        assert len(hub.peers) == 1

    asyncio.run(_test())


def test_live_share_ws_api_integration(share_client):
    """Test full WebSocket endpoint /api/share/live/ws with two concurrent client connections."""
    client, share_mgr = share_client

    with client.websocket_connect("/api/share/live/ws") as ws1:
        msg1 = ws1.receive_json()
        assert msg1["type"] == "peers"
        self_id1 = msg1["self_id"]

        with client.websocket_connect("/api/share/live/ws") as ws2:
            msg2 = ws2.receive_json()
            assert msg2["type"] == "peers"
            self_id2 = msg2["self_id"]
            assert self_id1 != self_id2

            # ws1 should have received updated peer list
            updated1 = ws1.receive_json()
            assert updated1["type"] == "peers"
            assert len(updated1["peers"]) == 2

            # Client 1 requests transfer to Client 2
            tid = "tid_abc123"
            ws1.send_json({
                "type": "transfer-request",
                "transfer_id": tid,
                "target_id": self_id2,
                "file_name": "direct_movie.mkv",
                "file_size": 1048576,
            })

            # Client 2 receives transfer request
            req = ws2.receive_json()
            assert req["type"] == "transfer-request"
            assert req["transfer_id"] == tid
            assert req["sender_id"] == self_id1
            assert req["file_name"] == "direct_movie.mkv"

            # Client 2 accepts
            ws2.send_json({
                "type": "transfer-response",
                "transfer_id": tid,
                "target_id": self_id1,
                "accepted": True,
            })

            # Client 1 receives accepted
            resp = ws1.receive_json()
            assert resp["type"] == "transfer-response"
            assert resp["transfer_id"] == tid
            assert resp["accepted"] is True

            # Client 1 streams binary chunks directly
            header = tid.encode("ascii").ljust(16, b" ")
            test_chunk = header + b"CHUNK_DATA_STREAM_BYTES"
            ws1.send_bytes(test_chunk)

            # Client 2 receives binary chunk
            recvd_bytes = ws2.receive_bytes()
            assert recvd_bytes == test_chunk

            # Verify that ZERO files were written to router storage
            file_entries = list(share_mgr.storage_dir.glob("*/"))
            for d in file_entries:
                assert d.name == "bundles" or not d.is_dir()


def test_live_share_query_param_name(share_client):
    """Test registering device name directly via query parameter in WebSocket URL."""
    client, _ = share_client
    with client.websocket_connect("/api/share/live/ws?name=GalaxyS24") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "peers"
        self_id = msg["self_id"]
        self_peer = next(p for p in msg["peers"] if p["id"] == self_id)
        assert self_peer["name"] == "GalaxyS24"


def test_live_share_error_on_receiver_drop(tmp_path):
    """Test that when receiver drops mid-stream, sender receives immediate transfer-error."""
    async def _test():
        hub = LocalShareManager(tmp_path / "hub_drop_test").live_hub

        class FakeWS:
            def __init__(self):
                self.sent_json = []
                self.sent_bytes = []
                self.should_fail = False
            async def send_json(self, msg):
                self.sent_json.append(msg)
            async def send_bytes(self, b):
                if self.should_fail:
                    raise ConnectionResetError("Socket closed")
                self.sent_bytes.append(b)

        sender_ws = FakeWS()
        receiver_ws = FakeWS()
        await hub.register_peer("sender_p", "192.168.1.50", "Sender", sender_ws)
        await hub.register_peer("receiver_p", "192.168.1.60", "Receiver", receiver_ws)
        await hub.register_transfer("transfer_drop", "sender_p", "receiver_p")

        # Now receiver drops
        receiver_ws.should_fail = True
        header = "transfer_drop".encode("ascii").ljust(16, b" ")
        chunk = header + b"CHUNK"

        routed = await hub.route_binary_chunk(chunk)
        assert routed is False
        # Sender must be notified immediately
        assert any(m.get("type") == "transfer-error" for m in sender_ws.sent_json)
        assert "transfer_drop" not in hub.active_transfers

    asyncio.run(_test())


def test_live_share_http_stream_download(share_client):
    """Test zero-RAM HTTP streaming download endpoint /api/share/live/download/{transfer_id}."""
    client, share_mgr = share_client

    with client.websocket_connect("/api/share/live/ws") as ws1:
        msg1 = ws1.receive_json()
        self_id1 = msg1["self_id"]

        with client.websocket_connect("/api/share/live/ws") as ws2:
            msg2 = ws2.receive_json()
            self_id2 = msg2["self_id"]
            ws1.receive_json()  # peer update

            tid = "t_stream_999"
            ws1.send_json({
                "type": "transfer-request",
                "transfer_id": tid,
                "target_id": self_id2,
                "file_name": "test_bundle.zip",
                "file_size": 34,
                "file_type": "application/zip",
            })
            req = ws2.receive_json()
            assert req["transfer_id"] == tid

            # Accept
            ws2.send_json({
                "type": "transfer-response",
                "transfer_id": tid,
                "target_id": self_id1,
                "accepted": True,
            })
            resp = ws1.receive_json()
            assert resp["accepted"] is True

            # Client 1 sends a chunk
            header = tid.encode("ascii").ljust(16, b" ")
            test_payload = b"DIRECT_ZERO_RAM_STREAM_DATA_123456"

            # Create stream queue to enable HTTP stream mode
            asyncio.run(share_mgr.live_hub.get_or_create_stream_queue(tid))

            ws1.send_bytes(header + test_payload)

            # Signal done
            ws1.send_json({
                "type": "transfer-done",
                "transfer_id": tid,
                "target_id": self_id2,
            })

            # Check HTTP download response
            download_resp = client.get(f"/api/share/live/download/{tid}")
            assert download_resp.status_code == 200
            assert "attachment; filename=\"test_bundle.zip\"" in download_resp.headers["content-disposition"]
            assert download_resp.content == test_payload




def test_live_share_50mb_size_limit(share_client):
    """Test that Live Direct Transfer requests exceeding 50 MB are rejected."""
    client, _ = share_client

    with client.websocket_connect("/api/share/live/ws") as ws1:
        msg1 = ws1.receive_json()
        self_id1 = msg1["self_id"]

        with client.websocket_connect("/api/share/live/ws") as ws2:
            msg2 = ws2.receive_json()
            self_id2 = msg2["self_id"]
            ws1.receive_json()  # peer update

            # Request 51 MB transfer
            oversized = 51 * 1024 * 1024
            ws1.send_json({
                "type": "transfer-request",
                "transfer_id": "t_too_big",
                "target_id": self_id2,
                "file_name": "huge_movie.mp4",
                "file_size": oversized,
                "file_type": "video/mp4",
            })

            # Sender ws1 should receive transfer-error immediately
            err = ws1.receive_json()
            assert err["type"] == "transfer-error"
            assert "50 MB" in err["message"]




