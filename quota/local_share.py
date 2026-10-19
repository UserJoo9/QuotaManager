"""Local Share engine for QuotaManager.

Provides fast, private, zero-configuration local file and text/link sharing
across any device (phones, PCs, tablets, smart TVs) connected to the local home network.
Stores files in a local cache with automatic TTL expiration and low memory footprint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("quota.local_share")

DEFAULT_TTL_SECONDS = 7200  # 2 hours
MAX_TEXT_ITEMS = 50
MAX_TEXT_LENGTH = 10000
MAX_LIVE_DIRECT_BYTES = 50 * 1024 * 1024  # 50 MB


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent directory traversal and invalid characters."""
    base = Path(filename).name.strip()
    if not base:
        base = "shared_file"
    # Replace dangerous or problematic characters
    clean = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", base)
    # Clamp length
    if len(clean) > 180:
        stem = Path(clean).stem[:160]
        ext = Path(clean).suffix[:20]
        clean = f"{stem}{ext}"
    return clean or "shared_file"


def sanitize_relpath(path: str) -> str:
    """Sanitize relative path within a directory/zip archive."""
    normalized = path.replace("\\", "/").strip().lstrip("/")
    parts = [p for p in normalized.split("/") if p and p != ".."]
    clean_parts = [re.sub(r'[\\*?:"<>|\x00-\x1f]', "_", p) for p in parts]
    return "/".join(clean_parts) or "file.bin"


@dataclass
class SharedFile:
    id: str
    name: str
    size: int
    content_type: str
    uploaded_at: float
    expires_at: float
    sender_ip: str
    download_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "content_type": self.content_type,
            "uploaded_at": self.uploaded_at,
            "expires_at": self.expires_at,
            "sender_ip": self.sender_ip,
            "download_count": self.download_count,
            "is_expired": time.time() > self.expires_at,
        }


@dataclass
class SharedText:
    id: str
    text: str
    created_at: float
    sender_ip: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "created_at": self.created_at,
            "sender_ip": self.sender_ip,
        }


class LocalShareManager:
    """Thread-safe manager for locally shared files and text notes."""

    def __init__(self, storage_dir: Path | str) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.storage_dir / "index.json"
        self._lock = asyncio.Lock()
        self.files: dict[str, SharedFile] = {}
        self.texts: list[SharedText] = []
        self._folder_sessions: dict[str, dict[str, Any]] = {}
        self.live_hub = LiveShareHub()
        self._load_index()

    def _load_index(self) -> None:
        """Load persisted metadata from disk."""
        if not self.index_file.exists():
            return
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            for item in data.get("files", []):
                sf = SharedFile(
                    id=item["id"],
                    name=item["name"],
                    size=item.get("size", 0),
                    content_type=item.get("content_type", "application/octet-stream"),
                    uploaded_at=item.get("uploaded_at", now),
                    expires_at=item.get("expires_at", now + DEFAULT_TTL_SECONDS),
                    sender_ip=item.get("sender_ip", "127.0.0.1"),
                    download_count=item.get("download_count", 0),
                )
                file_path = self.storage_dir / sf.id / sf.name
                if file_path.exists() and sf.expires_at > now:
                    self.files[sf.id] = sf
                elif sf.expires_at <= now:
                    # Clean up expired on boot
                    target_dir = self.storage_dir / sf.id
                    if target_dir.exists():
                        shutil.rmtree(target_dir, ignore_errors=True)

            for t in data.get("texts", []):
                self.texts.append(
                    SharedText(
                        id=t["id"],
                        text=t["text"],
                        created_at=t.get("created_at", now),
                        sender_ip=t.get("sender_ip", "127.0.0.1"),
                    )
                )
        except Exception as exc:
            log.warning("Could not read local share index: %s", exc)

    def _save_index(self) -> None:
        """Persist metadata to disk atomically."""
        try:
            payload = {
                "files": [sf.to_dict() for sf in self.files.values()],
                "texts": [st.to_dict() for st in self.texts],
            }
            tmp = self.storage_dir / "index.json.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            tmp.replace(self.index_file)
        except Exception as exc:
            log.warning("Could not save local share index: %s", exc)

    async def save_file_stream(
        self,
        stream: Any,
        filename: str,
        content_type: str,
        sender_ip: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> SharedFile:
        """Stream an incoming file to disk with low memory usage."""
        file_id = secrets.token_hex(6)
        safe_name = sanitize_filename(filename)
        dest_dir = self.storage_dir / file_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / safe_name

        total_bytes = 0
        with open(dest_file, "wb") as f:
            if hasattr(stream, "__aiter__"):
                async for chunk in stream:
                    if chunk:
                        f.write(chunk)
                        total_bytes += len(chunk)
            elif callable(stream):
                while True:
                    chunk = await stream(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    total_bytes += len(chunk)
            elif hasattr(stream, "read"):
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    total_bytes += len(chunk)

        now = time.time()
        sf = SharedFile(
            id=file_id,
            name=safe_name,
            size=total_bytes,
            content_type=content_type or "application/octet-stream",
            uploaded_at=now,
            expires_at=now + max(60, ttl_seconds),
            sender_ip=sender_ip,
            download_count=0,
        )

        async with self._lock:
            self.files[file_id] = sf
            self._save_index()

        log.info("Local share: saved '%s' (%d bytes) from %s", safe_name, total_bytes, sender_ip)
        return sf

    async def init_folder_upload(
        self,
        folder_name: str,
        sender_ip: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        """Initialize an on-the-fly streaming ZIP archive for a whole directory upload."""
        folder_id = secrets.token_hex(6)
        clean_base = sanitize_filename(folder_name)
        if not clean_base.lower().endswith(".zip"):
            clean_name = f"{clean_base}.zip"
        else:
            clean_name = clean_base

        dest_dir = self.storage_dir / folder_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        zip_path = dest_dir / clean_name

        # Create writable zip file
        zf = zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED)
        now = time.time()

        async with self._lock:
            self._folder_sessions[folder_id] = {
                "folder_name": clean_name,
                "zip_path": zip_path,
                "zip_file": zf,
                "sender_ip": sender_ip,
                "ttl_seconds": max(60, ttl_seconds),
                "created_at": now,
                "file_count": 0,
            }
        log.info("Local share: initiated folder archive '%s' (%s) from %s", clean_name, folder_id, sender_ip)
        return folder_id

    async def append_folder_file(
        self,
        folder_id: str,
        relpath: str,
        stream: Any,
    ) -> int:
        """Stream a single file into the active in-progress folder ZIP archive."""
        session = self._folder_sessions.get(folder_id)
        if not session:
            raise ValueError(f"Folder upload session '{folder_id}' not found or already finalized")

        zf: zipfile.ZipFile = session["zip_file"]
        clean_rel = sanitize_relpath(relpath)

        total_bytes = 0
        with zf.open(clean_rel, "w") as out_f:
            if hasattr(stream, "__aiter__"):
                async for chunk in stream:
                    if chunk:
                        out_f.write(chunk)
                        total_bytes += len(chunk)
            elif callable(stream):
                while True:
                    chunk = await stream(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    total_bytes += len(chunk)
            elif hasattr(stream, "read"):
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    total_bytes += len(chunk)

        session["file_count"] += 1
        return total_bytes

    async def finalize_folder_upload(self, folder_id: str) -> SharedFile:
        """Close the ZIP archive, record it as a shared file item, and save index."""
        async with self._lock:
            session = self._folder_sessions.pop(folder_id, None)
            if not session:
                raise ValueError(f"Folder session '{folder_id}' not found")

            zf: zipfile.ZipFile = session["zip_file"]
            try:
                zf.close()
            except Exception as exc:
                log.warning("Error closing folder zip '%s': %s", folder_id, exc)

            zip_path: Path = session["zip_path"]
            total_size = zip_path.stat().st_size if zip_path.exists() else 0
            now = time.time()
            ttl = session["ttl_seconds"]

            sf = SharedFile(
                id=folder_id,
                name=session["folder_name"],
                size=total_size,
                content_type="application/zip",
                uploaded_at=now,
                expires_at=now + ttl,
                sender_ip=session["sender_ip"],
                download_count=0,
            )
            self.files[folder_id] = sf
            self._save_index()

        log.info(
            "Local share: finalized folder '%s' (%d files, %d bytes) from %s",
            sf.name,
            session["file_count"],
            total_size,
            sf.sender_ip,
        )
        return sf

    async def create_batch_zip(self, file_ids: list[str]) -> Optional[tuple[Path, str]]:
        """Bundle multiple selected files into a single ZIP archive on demand."""
        async with self._lock:
            valid_files: list[tuple[Path, str]] = []
            for fid in file_ids:
                sf = self.files.get(fid)
                if sf and time.time() <= sf.expires_at:
                    fpath = self.storage_dir / sf.id / sf.name
                    if fpath.exists():
                        valid_files.append((fpath, sf.name))

            if not valid_files:
                return None

            bundles_dir = self.storage_dir / "bundles"
            bundles_dir.mkdir(parents=True, exist_ok=True)
            bundle_name = f"LocalShare_Batch_{secrets.token_hex(4)}.zip"
            bundle_path = bundles_dir / bundle_name

            seen_names: dict[str, int] = {}
            with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fpath, fname in valid_files:
                    arcname = fname
                    if arcname in seen_names:
                        seen_names[arcname] += 1
                        stem = Path(fname).stem
                        ext = Path(fname).suffix
                        arcname = f"{stem}_{seen_names[fname]}{ext}"
                    else:
                        seen_names[arcname] = 0
                    zf.write(fpath, arcname=arcname)

            return bundle_path, "LocalShare_Bundle.zip"

    async def delete_batch(self, file_ids: list[str]) -> int:
        """Delete multiple files in one batch operation."""
        async with self._lock:
            count = 0
            for fid in file_ids:
                if await self._delete_file_internal(fid):
                    count += 1
            return count

    async def get_file(self, file_id: str) -> Optional[tuple[Path, str, str]]:
        """Return (path, filename, content_type) or None if missing/expired."""
        async with self._lock:
            sf = self.files.get(file_id)
            if not sf:
                return None
            if time.time() > sf.expires_at:
                await self._delete_file_internal(file_id)
                return None
            file_path = self.storage_dir / sf.id / sf.name
            if not file_path.exists():
                del self.files[file_id]
                self._save_index()
                return None
            sf.download_count += 1
            self._save_index()
            return file_path, sf.name, sf.content_type

    async def _delete_file_internal(self, file_id: str) -> bool:
        if file_id in self.files:
            sf = self.files.pop(file_id)
            target_dir = self.storage_dir / sf.id
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            self._save_index()
            return True
        return False

    async def delete_file(self, file_id: str) -> bool:
        """Manually delete a shared file."""
        async with self._lock:
            return await self._delete_file_internal(file_id)

    async def save_text(self, text: str, sender_ip: str) -> SharedText:
        """Save a quick text/link snippet."""
        clean_text = text.strip()[:MAX_TEXT_LENGTH]
        text_id = secrets.token_hex(6)
        st = SharedText(
            id=text_id,
            text=clean_text,
            created_at=time.time(),
            sender_ip=sender_ip,
        )
        async with self._lock:
            self.texts.insert(0, st)
            if len(self.texts) > MAX_TEXT_ITEMS:
                self.texts = self.texts[:MAX_TEXT_ITEMS]
            self._save_index()
        log.info("Local share: text added (%d chars) from %s", len(clean_text), sender_ip)
        return st

    async def delete_text(self, text_id: str) -> bool:
        """Delete a shared text snippet."""
        async with self._lock:
            initial_len = len(self.texts)
            self.texts = [t for t in self.texts if t.id != text_id]
            if len(self.texts) != initial_len:
                self._save_index()
                return True
            return False

    async def cleanup_expired(self) -> int:
        """Delete files whose TTL has passed."""
        now = time.time()
        expired_ids = [fid for fid, sf in self.files.items() if sf.expires_at <= now]
        async with self._lock:
            count = 0
            for fid in expired_ids:
                if await self._delete_file_internal(fid):
                    count += 1
            bundles_dir = self.storage_dir / "bundles"
            if bundles_dir.exists():
                for b in bundles_dir.glob("*.zip"):
                    try:
                        if b.stat().st_mtime < now - 1800:
                            b.unlink()
                    except Exception:
                        pass
            return count

    async def clear_all(self) -> None:
        """Clear all shared files and text notes."""
        async with self._lock:
            for sf in list(self.files.values()):
                target_dir = self.storage_dir / sf.id
                if target_dir.exists():
                    shutil.rmtree(target_dir, ignore_errors=True)
            self.files.clear()
            self.texts.clear()
            self._save_index()

    async def list_all(self) -> dict[str, Any]:
        """Return list of active files and texts, sorted newest first."""
        await self.cleanup_expired()
        async with self._lock:
            files_list = sorted(
                [sf.to_dict() for sf in self.files.values()],
                key=lambda x: x["uploaded_at"],
                reverse=True,
            )
            texts_list = sorted(
                [st.to_dict() for st in self.texts],
                key=lambda x: x["created_at"],
                reverse=True,
            )
            total_size = sum(f["size"] for f in files_list)
            return {
                "files": files_list,
                "texts": texts_list,
                "total_files": len(files_list),
                "total_size": total_size,
            }


@dataclass
class LivePeer:
    peer_id: str
    client_ip: str
    device_name: str
    ws: Any
    connected_at: float


def detect_device_name(ua: str, host: str) -> str:
    """Derive a friendly device label from the HTTP User-Agent and host IP."""
    ua_lower = ua.lower()
    last_octet = host.split(".")[-1] if "." in host else host
    if "iphone" in ua_lower:
        return f"iPhone ({last_octet})"
    if "ipad" in ua_lower:
        return f"iPad ({last_octet})"
    if "android" in ua_lower:
        if "mobile" in ua_lower:
            return f"Android Phone ({last_octet})"
        return f"Android Tablet ({last_octet})"
    if "windows" in ua_lower:
        return f"Windows PC ({last_octet})"
    if "macintosh" in ua_lower or "mac os" in ua_lower:
        return f"Mac ({last_octet})"
    if "linux" in ua_lower:
        return f"Linux PC ({last_octet})"
    if "tizen" in ua_lower or "smart-tv" in ua_lower or "webos" in ua_lower:
        return f"Smart TV ({last_octet})"
    return f"Device ({last_octet})"


class LiveShareHub:
    """Manages active LAN peer connections and mediates zero-disk live stream relay."""

    def __init__(self) -> None:
        self.peers: dict[str, LivePeer] = {}
        # active transfers: transfer_id -> (sender_id, target_id)
        self.active_transfers: dict[str, tuple[str, str]] = {}
        # Fast header lookup: 16-byte raw prefix -> (sender_id, target_id, transfer_id)
        self._transfers_by_header: dict[bytes, tuple[str, str, str]] = {}
        # Metadata: transfer_id -> {"name": file_name, "size": file_size, "type": file_type}
        self.transfers_meta: dict[str, dict[str, Any]] = {}
        # Direct HTTP download streaming queues (zero JS heap RAM on receiver): transfer_id -> asyncio.Queue
        self.stream_queues: dict[str, asyncio.Queue] = {}
        self.bytes_transferred: dict[str, int] = {}
        self._last_progress_time: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def register_peer(self, peer_id: str, client_ip: str, device_name: str, ws: Any) -> None:
        async with self._lock:
            self.peers[peer_id] = LivePeer(
                peer_id=peer_id,
                client_ip=client_ip,
                device_name=device_name or f"Device ({client_ip})",
                ws=ws,
                connected_at=time.time(),
            )
        await self.broadcast_peer_list()

    async def unregister_peer(self, peer_id: str) -> None:
        async with self._lock:
            self.peers.pop(peer_id, None)
            to_remove = [
                tid for tid, (s, t) in self.active_transfers.items()
                if s == peer_id or t == peer_id
            ]
            for tid in to_remove:
                self.active_transfers.pop(tid, None)
                raw_key = tid.encode("ascii", errors="ignore").ljust(16, b" ")[:16]
                self._transfers_by_header.pop(raw_key, None)
                self.transfers_meta.pop(tid, None)
                q = self.stream_queues.pop(tid, None)
                if q is not None:
                    try:
                        q.put_nowait(None)
                    except Exception:
                        pass
                self.bytes_transferred.pop(tid, None)
                self._last_progress_time.pop(tid, None)
        await self.broadcast_peer_list()

    def get_peer_list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p.peer_id,
                "ip": p.client_ip,
                "name": p.device_name,
                "connected_at": p.connected_at,
            }
            for p in self.peers.values()
        ]

    async def broadcast_peer_list(self) -> None:
        """Send updated online peers list to all active connections."""
        peers_data = self.get_peer_list()
        dead = []
        for p in list(self.peers.values()):
            try:
                await p.ws.send_json({
                    "type": "peers",
                    "peers": peers_data,
                    "self_id": p.peer_id,
                    "self_ip": p.client_ip,
                })
            except Exception:
                dead.append(p.peer_id)
        for dead_id in dead:
            await self.unregister_peer(dead_id)

    async def send_to_peer(self, target_id: str, message: dict[str, Any]) -> bool:
        target = self.peers.get(target_id)
        if not target:
            return False
        try:
            await target.ws.send_json(message)
            return True
        except Exception:
            await self.unregister_peer(target_id)
            return False

    async def register_transfer(
        self,
        transfer_id: str,
        sender_id: str,
        target_id: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            self.active_transfers[transfer_id] = (sender_id, target_id)
            raw_key = transfer_id.encode("ascii", errors="ignore").ljust(16, b" ")[:16]
            self._transfers_by_header[raw_key] = (sender_id, target_id, transfer_id)
            if meta:
                self.transfers_meta[transfer_id] = meta
            self.bytes_transferred[transfer_id] = 0

    async def get_or_create_stream_queue(self, transfer_id: str) -> asyncio.Queue:
        async with self._lock:
            q = self.stream_queues.get(transfer_id)
            if q is None:
                q = asyncio.Queue(maxsize=32)
                self.stream_queues[transfer_id] = q
            return q

    async def finish_stream_queue(self, transfer_id: str) -> None:
        async with self._lock:
            q = self.stream_queues.get(transfer_id)
            if q is not None:
                await q.put(None)

    async def unregister_transfer(self, transfer_id: str) -> None:
        async with self._lock:
            self.active_transfers.pop(transfer_id, None)
            raw_key = transfer_id.encode("ascii", errors="ignore").ljust(16, b" ")[:16]
            self._transfers_by_header.pop(raw_key, None)
            self.transfers_meta.pop(transfer_id, None)
            q = self.stream_queues.pop(transfer_id, None)
            if q is not None:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self.bytes_transferred.pop(transfer_id, None)
            self._last_progress_time.pop(transfer_id, None)

    async def route_binary_chunk(self, chunk: bytes) -> bool:
        """Zero-disk streaming: route payload to direct HTTP download stream queue or target WebSocket."""
        if len(chunk) < 16:
            return False
        header = chunk[:16]
        entry = self._transfers_by_header.get(header)
        if entry:
            sender_id, target_id, transfer_id = entry
        else:
            transfer_id = header.decode("ascii", errors="ignore").replace("\x00", "").strip()
            pair = self.active_transfers.get(transfer_id)
            if not pair:
                return False
            sender_id, target_id = pair
            self._transfers_by_header[header] = (sender_id, target_id, transfer_id)

        payload = chunk[16:]
        stream_q = self.stream_queues.get(transfer_id)

        # Track transferred bytes
        total_b = self.bytes_transferred.get(transfer_id, 0) + len(payload)
        self.bytes_transferred[transfer_id] = total_b

        # If HTTP stream queue is active (zero RAM in browser tab):
        if stream_q is not None:
            await stream_q.put(payload)
            # Emit throttled progress JSON to receiver WebSocket while downloading via HTTP
            now = time.time()
            if now - self._last_progress_time.get(transfer_id, 0) > 0.15:
                self._last_progress_time[transfer_id] = now
                target = self.peers.get(target_id)
                if target:
                    meta = self.transfers_meta.get(transfer_id, {})
                    try:
                        await target.ws.send_json({
                            "type": "transfer-progress",
                            "transfer_id": transfer_id,
                            "loaded": total_b,
                            "total": meta.get("size", 0),
                        })
                    except Exception:
                        pass
            return True

        # Fallback: Forward raw WebSocket binary chunk
        target = self.peers.get(target_id)
        if not target:
            sender = self.peers.get(sender_id)
            if sender:
                try:
                    await sender.ws.send_json({
                        "type": "transfer-error",
                        "transfer_id": transfer_id,
                        "message": "Receiver device disconnected during transfer.",
                    })
                except Exception:
                    pass
            await self.unregister_transfer(transfer_id)
            return False
        try:
            await target.ws.send_bytes(chunk)
            return True
        except Exception:
            await self.unregister_peer(target_id)
            sender = self.peers.get(sender_id)
            if sender:
                try:
                    await sender.ws.send_json({
                        "type": "transfer-error",
                        "transfer_id": transfer_id,
                        "message": "Receiver connection lost during transfer.",
                    })
                except Exception:
                    pass
            await self.unregister_transfer(transfer_id)
            return False

