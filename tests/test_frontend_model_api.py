"""Frontend model/api mapper characterization (executed via Node)."""
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "frontend_model_api_char.mjs"


def test_frontend_model_api_mappers():
    node = shutil.which("node")
    if not node:
        import pytest

        pytest.skip("node not available")
    res = subprocess.run([node, str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + "\n" + res.stderr
    assert "OK" in res.stdout
