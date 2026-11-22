"""Turn the whole gateway off from the dashboard (System > Power).

The work is done by ``scripts/gateway_off.sh``; this only schedules it through
``systemd-run`` a few seconds out, so the API can answer first and the script
keeps running after it stops ``quota-gateway.service`` (this very process).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger("quota.power")

RunCommand = Callable[[list[str]], tuple[int, str]]

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gateway_off.sh"
DELAY_S = 3


def _default_run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return 127, f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def schedule_gateway_off(poweroff: bool = False,
                         script: Path = SCRIPT,
                         run_command: RunCommand | None = None) -> dict[str, object]:
    """Schedule the gateway shutdown. Raises RuntimeError when it cannot."""
    if not script.exists():
        raise RuntimeError(f"shutdown script not found: {script}")
    argv = ["systemd-run", "--unit=quota-gateway-off", "--collect",
            f"--on-active={DELAY_S}", "/bin/bash", str(script)]
    if poweroff:
        argv.append("poweroff")
    rc, out = (run_command or _default_run)(argv)
    if rc != 0:
        raise RuntimeError(out or f"systemd-run exited {rc}")
    log.warning("gateway turn-off scheduled in %d s (poweroff=%s)", DELAY_S, poweroff)
    return {"scheduled": True, "poweroff": poweroff, "delay_s": DELAY_S}
