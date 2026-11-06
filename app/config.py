from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


APP_NAME = "ProcOrbit"


def app_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    path = app_dir() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_PATH = app_dir() / "config.json"
DB_PATH = data_dir() / "pythonwatch.db"
HEARTBEAT_DIR = app_dir() / "heartbeats"


DEFAULT_CONFIG: dict[str, Any] = {
    "poll_interval_sec": 2.0,
    "hidden_poll_interval_sec": 3.0,
    "history_limit": 200,
    "db_sample_interval_sec": 10.0,
    "sample_retention_days": 7,
    "notification_cooldown_sec": 90,
    "notifications_enabled": True,
    # Detailed script/command/path metadata in Python history requires opt-in.
    "persist_python_metadata": False,
    "launch_hidden": True,
    "window_geometry": None,
    "watch_python_names": [
        "python.exe",
        "pythonw.exe",
        "python3.exe",
        "python3.12.exe",
        "py.exe"
    ],
    "health": {
        "cpu_watch_pct": 50.0,
        "cpu_high_pct": 75.0,
        "cpu_critical_pct": 90.0,
        "cpu_watch_sustain_sec": 5.0,
        "cpu_high_sustain_sec": 8.0,
        "cpu_sustain_sec": 10.0,
        "ram_watch_mb": 2048.0,
        "ram_high_mb": 4096.0,
        "ram_critical_mb": 8192.0,
        "memory_growth_high_mb_per_min": 1024.0
    }
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return _deep_merge(DEFAULT_CONFIG, raw)
    except Exception:
        # Keep the monitor alive even if the user hand-edits malformed JSON.
        return DEFAULT_CONFIG.copy()


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
