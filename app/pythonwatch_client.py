"""
Optional tiny progress/heartbeat helper for scripts you control.

Usage:
    from pythonwatch_client import report_progress

    report_progress(10, "loading", "Reading input files")
    ...
    report_progress(80, "backtest", "Running fold 8/10")

ProcOrbit can always see process CPU/RAM/time/command.
This helper is only needed if you also want semantic progress ("what step is it on?").
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _heartbeat_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / "ProcOrbit" / "heartbeats"
    path.mkdir(parents=True, exist_ok=True)
    return path


def report_progress(
    progress_pct: float | None = None,
    phase: str | None = None,
    message: str | None = None,
) -> None:
    payload = {
        "pid": os.getpid(),
        "ts": time.time(),
        "progress_pct": progress_pct,
        "phase": phase,
        "message": message,
    }
    path = _heartbeat_dir() / f"{os.getpid()}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
