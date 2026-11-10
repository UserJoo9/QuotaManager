"""Run the real dashboard/API locally without gateway network services.

Usage: .venv/Scripts/python scripts/run_local.py --port 8080
Data is kept in data/local-test, separate from the gateway database.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn

from api.app import create_app
from quota.db import Database
from quota.engine import SnapshotHolder
from quota.local_share import LocalShareManager
from quota.service import QuotaService


async def serve(port: int, data: Path) -> None:
    data = data.resolve()
    data.mkdir(parents=True, exist_ok=True)
    database = Database(data / "quota.db")
    await database.connect()
    try:
        service = QuotaService(database, timezone="Africa/Cairo")
        await service.ensure_period()
        app = create_app(
            database, service, SnapshotHolder(),
            local_share_manager=LocalShareManager(data / "share"),
        )
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port))
        await server.serve()
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "local-test",
                        help="Database/share directory; use a separate directory for automated tests")
    args = parser.parse_args()
    asyncio.run(serve(args.port, args.data_dir))
