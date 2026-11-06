from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
from PySide6.QtCore import QThread, Signal

from config import DB_PATH, HEARTBEAT_DIR, load_config
from health_score import score_run_health
from process_inventory import (
    ProcessInventory,
    ProcessMeasurements,
    ProcessRecord as InventoryProcessRecord,
)
from process_privacy import history_metadata
from storage import Storage


SEVERITY_ORDER = {"HEALTHY": 0, "WATCH": 1, "HIGH": 2, "CRITICAL": 3}
MAX_TRACKED_TREE_IDENTITIES = 512
Identity = tuple[int, float | None]


def worst(a: str, b: str) -> str:
    return a if SEVERITY_ORDER.get(a, 0) >= SEVERITY_ORDER.get(b, 0) else b


def safe_call(fn, default=None):
    try:
        return fn()
    except (psutil.Error, OSError, ProcessLookupError):
        return default


def read_field(fn) -> tuple[Any, str | None]:
    try:
        value = fn()
        return value, None if value is not None else "Unavailable"
    except psutil.AccessDenied:
        return None, "Access denied"
    except (psutil.Error, OSError, ProcessLookupError):
        return None, "Unavailable"


def script_info(argv: list[str] | None, process_name: str, cwd: str | None):
    if argv is None:
        return f"{process_name} (command unavailable)", None, None, cwd
    args = list(argv or [])
    target = None
    module = None
    kind = None
    i = 1 if args else 0
    value_options = {"-W", "-X", "--check-hash-based-pycs"}

    while i < len(args):
        part = str(args[i])
        if part == "-m" and i + 1 < len(args):
            module = str(args[i + 1])
            kind = "module"
            break
        if part == "-c":
            kind = "command"
            break
        if part == "--":
            i += 1
            if i < len(args):
                target = str(args[i])
                kind = "script"
            break
        if part in value_options:
            i += 2
            continue
        if part.startswith("-"):
            i += 1
            continue
        target = part
        kind = "script"
        break

    script_path = None
    if target:
        try:
            candidate = Path(target)
            if not candidate.is_absolute() and cwd:
                candidate = Path(cwd) / candidate
            script_path = str(candidate.resolve(strict=False))
        except (OSError, ValueError):
            script_path = target

    if kind == "module" and module:
        label = module
    elif kind == "script" and target:
        label = os.path.basename(target) or target
    elif kind == "command":
        label = f"{process_name} -c"
    else:
        label = "Interactive Python" if len(args) <= 1 else (process_name or "python")
    project = cwd or (str(Path(script_path).parent) if script_path else None)
    return label, script_path, module, project


def normalize_cmdline(argv: list[str] | None) -> str | None:
    if argv is None:
        return None
    try:
        return subprocess.list2cmdline([str(x) for x in argv])
    except (TypeError, ValueError):
        return " ".join(str(x) for x in argv)


@dataclass
class ProcessRecord:
    proc: psutil.Process
    pid: int
    ppid: int | None
    name: str
    created_at: float | None
    readings: ProcessMeasurements | None = None
    sample_metrics: bool = True


@dataclass
class ProcState:
    proc: psutil.Process
    key: Identity
    session_id: int | None
    meta: dict[str, Any]
    first_seen: float
    last_seen: float
    last_db_sample: float = 0.0
    sample_count: int = 0
    sum_cpu: float = 0.0
    sum_rss: float = 0.0
    max_cpu: float = 0.0
    max_rss: float = 0.0
    peak_mem_pct: float = 0.0
    health_worst: str = "HEALTHY"
    cpu_since: dict[str, float] = field(default_factory=dict)
    mem_history: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=60))
    alert_last_at: dict[str, float] = field(default_factory=dict)
    tree_members: list[tuple[int, float | None]] = field(default_factory=list)
    tree_members_complete: bool = True
    monitor_interrupted: bool = False


class MonitorWorker(QThread):
    snapshot = Signal(object, object)
    process_snapshot_ready = Signal(object)
    history_page_ready = Signal(object)
    alert = Signal(str, str)
    monitor_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_requested = False
        self._paused = False
        self._visible = False
        self.cfg = load_config()
        self.cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
        self.states: dict[Identity, ProcState] = {}
        self._cpu_primed: set[Identity] = set()
        self._io_baselines: dict[Identity, tuple[float, int, int]] = {}
        self._metadata_cache: dict[Identity, tuple[float, dict[str, Any]]] = {}
        self._pending_by_key: dict[Identity, dict[str, Any]] = {}
        self._pending_unknown: list[dict[str, Any]] = []
        self._pending_loaded = False
        self._history_requests: queue.Queue = queue.Queue(maxsize=4)
        self._self_pid = os.getpid()
        self._self_script_path = self._resolve_self_script()
        self._self_launcher_identities = self._find_self_launchers()
        self._process_inventory = ProcessInventory(cpu_count=self.cpu_count)
        self._universal_sample_tick = 0
        self._enumeration_complete = True
        self._enumeration_permission_denied = 0
        HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)

    def stop(self):
        self._stop_requested = True

    def set_paused(self, paused: bool):
        self._paused = paused

    def set_visible(self, visible: bool):
        self._visible = visible

    def request_history_page(
        self,
        generation: int,
        cursor: tuple[float, int] | None,
        limit: int = 50,
    ) -> bool:
        try:
            self._history_requests.put_nowait((generation, cursor, limit))
            return True
        except queue.Full:
            return False

    def _serve_history_requests(self, storage) -> None:
        while True:
            try:
                generation, cursor, limit = self._history_requests.get_nowait()
            except queue.Empty:
                return
            try:
                page = (
                    storage.history_page(cursor=cursor, limit=limit)
                    if storage is not None else
                    {
                        "rows": [], "has_more": False, "next_cursor": None,
                        "error": "History database is unavailable.",
                    }
                )
                page["generation"] = generation
                page["cursor"] = cursor
                self.history_page_ready.emit(page)
            except (sqlite3.Error, OSError) as exc:
                self.monitor_error.emit(
                    f"SQLite history: {type(exc).__name__}: {exc}"
                )
                self.history_page_ready.emit({
                    "rows": [], "has_more": False, "next_cursor": None,
                    "generation": generation, "cursor": cursor, "error": str(exc),
                })

    def _db_call(self, storage, method: str, *args, default=None, **kwargs):
        if storage is None:
            return default
        try:
            return getattr(storage, method)(*args, **kwargs)
        except (sqlite3.Error, OSError) as exc:
            self.monitor_error.emit(f"SQLite {method}: {type(exc).__name__}: {exc}")
            return default

    def _load_pending(self, storage) -> None:
        rows = self._db_call(storage, "open_sessions", default=[])
        self._pending_by_key.clear()
        self._pending_unknown.clear()
        for row in rows:
            pid = row.get("pid")
            created_at = row.get("created_at")
            if pid is None or created_at is None:
                self._pending_unknown.append(row)
                continue
            try:
                self._pending_by_key[(int(pid), float(created_at))] = row
            except (TypeError, ValueError):
                self._pending_unknown.append(row)
        self._pending_loaded = True

    def run(self):
        storage = None
        try:
            psutil.cpu_percent(None)
            while not self._stop_requested:
                if storage is None:
                    try:
                        storage = Storage(DB_PATH)
                        self._load_pending(storage)
                        self._db_call(
                            storage,
                            "cleanup_samples",
                            int(self.cfg["sample_retention_days"]),
                        )
                    except (sqlite3.Error, OSError) as exc:
                        self.monitor_error.emit(
                            f"SQLite startup: {type(exc).__name__}: {exc}"
                        )
                        storage = None

                if not self._paused:
                    try:
                        self._scan(storage)
                    except Exception as exc:
                        self.monitor_error.emit(f"{type(exc).__name__}: {exc}")

                self._serve_history_requests(storage)

                interval_key = (
                    "poll_interval_sec" if self._visible
                    else "hidden_poll_interval_sec"
                )
                interval = float(self.cfg.get(interval_key, 2.0))
                self.msleep(max(200, int(interval * 1000)))
        finally:
            # Persist the last observation, but leave live processes open for
            # exact PID + creation-time reconciliation on the next launch.
            now = time.time()
            if storage is not None:
                for state in list(self.states.values()):
                    self._persist_summary(storage, state, now)
                try:
                    storage.close()
                except sqlite3.Error:
                    pass

    def _watch_name(self, name: str | None) -> bool:
        n = (name or "").lower()
        configured = {str(x).lower() for x in self.cfg["watch_python_names"]}
        return n in configured or n.startswith("python")

    @staticmethod
    def _identity(pid: int, created_at: float | None) -> Identity:
        return pid, created_at

    def _resolve_self_script(self) -> str | None:
        if not sys.argv or sys.argv[0] in {"-", "-c"}:
            return None
        try:
            return str(Path(sys.argv[0]).resolve(strict=False))
        except (OSError, ValueError):
            return None

    def _find_self_launchers(self) -> dict[int, float]:
        """Skip Python shim parents that are executing this same script."""
        if not self._self_script_path:
            return {}
        expected = os.path.normcase(self._self_script_path)
        launchers: dict[int, float] = {}
        visited = {self._self_pid}
        current = safe_call(lambda: psutil.Process(self._self_pid), None)
        while current is not None:
            parent = safe_call(current.parent, None)
            if parent is None or parent.pid in visited:
                break
            visited.add(parent.pid)
            name = safe_call(parent.name, None)
            if not self._watch_name(name):
                break
            created_at = safe_call(parent.create_time, None)
            argv = safe_call(parent.cmdline, None)
            cwd = safe_call(parent.cwd, None)
            _label, script_path, _module, _project = script_info(
                argv, name or "python", cwd
            )
            if (
                created_at is None
                or not script_path
                or os.path.normcase(script_path) != expected
            ):
                break
            launchers[parent.pid] = float(created_at)
            current = parent
        return launchers

    def _process_snapshot(
        self,
        *,
        include_internal: bool = False,
        sample_metrics: bool = True,
    ) -> dict[int, ProcessRecord]:
        records: dict[int, ProcessRecord] = {}
        complete = True
        permission_denied = 0
        denied_value = object()
        attributes = ["pid", "ppid", "name", "create_time"]
        if include_internal and sample_metrics:
            # Process.as_dict reads these inside psutil's oneshot context,
            # avoiding separate Windows process-handle queries later.
            attributes.extend(["cpu_times", "memory_info"])
        try:
            processes = psutil.process_iter(
                attrs=attributes, ad_value=denied_value
            )
            for proc in processes:
                try:
                    info = proc.info
                except psutil.AccessDenied:
                    complete = False
                    permission_denied += 1
                    continue
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    complete = False
                    continue
                except (psutil.Error, OSError, AttributeError, TypeError):
                    complete = False
                    continue
                try:
                    denied_fields = {
                        key for key in attributes
                        if info.get(key) is denied_value
                    }
                    permission_denied += len(denied_fields)
                    info = {
                        key: (None if value is denied_value else value)
                        for key, value in info.items()
                    }
                    pid = int(info.get("pid") or proc.pid)
                    if not include_internal and pid == self._self_pid:
                        continue
                    raw_name = info.get("name")
                    if raw_name is None and "name" not in denied_fields:
                        raw_name = safe_call(proc.name, None)
                    name = str(raw_name) if raw_name else "Unknown process"
                    ppid = info.get("ppid")
                    try:
                        ppid = int(ppid) if ppid is not None else None
                    except (TypeError, ValueError):
                        ppid = None
                    created = info.get("create_time")
                    try:
                        created = float(created) if created is not None else None
                    except (TypeError, ValueError):
                        created = None
                    if created is None and "create_time" not in denied_fields:
                        value = safe_call(proc.create_time, None)
                        try:
                            created = float(value) if value is not None else None
                        except (TypeError, ValueError):
                            created = None
                    readings = None
                    if include_internal and sample_metrics:
                        cpu_times = info.get("cpu_times")
                        cpu_status = None
                        cpu_total = None
                        if "cpu_times" in denied_fields:
                            cpu_status = "Access denied"
                        elif cpu_times is not None:
                            try:
                                cpu_total = float(cpu_times.user) + float(cpu_times.system)
                            except (AttributeError, TypeError, ValueError):
                                cpu_status = "Unavailable"
                        else:
                            cpu_status = "Unavailable"

                        memory = info.get("memory_info")
                        memory_status = None
                        memory_rss = None
                        if "memory_info" in denied_fields:
                            memory_status = "Access denied"
                        elif memory is not None:
                            try:
                                memory_rss = int(memory.rss)
                            except (AttributeError, TypeError, ValueError):
                                memory_status = "Unavailable"
                        else:
                            memory_status = "Unavailable"
                        readings = ProcessMeasurements(
                            cpu_total_sec=cpu_total,
                            cpu_status=cpu_status,
                            memory_rss_bytes=memory_rss,
                            memory_status=memory_status,
                        )
                    launcher_created = self._self_launcher_identities.get(pid)
                    if (
                        not include_internal
                        and launcher_created is not None
                        and created is not None
                        and abs(launcher_created - created) <= 0.01
                    ):
                        continue
                    records[pid] = ProcessRecord(
                        proc, pid, ppid, name, created, readings, sample_metrics
                    )
                except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
                    complete = False
                    continue
                except (TypeError, ValueError, AttributeError):
                    complete = False
                    continue
                except Exception:
                    # Preserve the monitor loop when a process exposes an
                    # unexpected metadata failure; absence stays uncertain.
                    complete = False
                    continue
        except psutil.AccessDenied:
            complete = False
            permission_denied += 1
        except (psutil.Error, OSError, TypeError):
            complete = False

        self._enumeration_complete = complete
        self._enumeration_permission_denied = permission_denied
        return records

    def _children_map(
        self, records: dict[int, ProcessRecord]
    ) -> dict[int, list[int]]:
        children: dict[int, list[int]] = defaultdict(list)
        for record in records.values():
            if record.ppid is None or record.ppid not in records:
                continue
            parent = records[record.ppid]
            # Reject stale/reused-PID parent links when timestamps prove that
            # the supposed parent was created after this child.
            if (
                parent.created_at is not None
                and record.created_at is not None
                and parent.created_at > record.created_at
            ):
                continue
            children[parent.pid].append(record.pid)
        for pid in children:
            children[pid].sort(key=lambda child: (records[child].name.lower(), child))
        return children

    def _python_ancestry(
        self,
        record: ProcessRecord,
        records: dict[int, ProcessRecord],
        python_pids: set[int],
    ) -> tuple[int, int | None, bool]:
        """Return top Python ancestor, nearest Python parent, and uncertainty."""
        current = record.ppid
        seen = {record.pid}
        python_ancestors: list[int] = []
        uncertain = False
        while current is not None:
            if current in seen:
                uncertain = True
                break
            seen.add(current)
            parent = records.get(current)
            if parent is None:
                uncertain = True
                break
            if (
                parent.created_at is not None
                and record.created_at is not None
                and parent.created_at > record.created_at
            ):
                uncertain = True
                break
            if current in python_pids:
                python_ancestors.append(current)
            current = parent.ppid
        if python_ancestors:
            return python_ancestors[-1], python_ancestors[0], uncertain
        return record.pid, None, uncertain

    def _subtree(
        self,
        root_pid: int,
        records: dict[int, ProcessRecord],
        children: dict[int, list[int]],
    ) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = [(root_pid, 0)]
        visited = {root_pid}
        stack = [(child, 1) for child in reversed(children.get(root_pid, []))]
        while stack:
            pid, depth = stack.pop()
            if pid in visited or pid not in records:
                continue
            visited.add(pid)
            result.append((pid, depth))
            for child in reversed(children.get(pid, [])):
                if child not in visited:
                    stack.append((child, depth + 1))
        return result

    def _static_metadata(
        self,
        record: ProcessRecord,
        records: dict[int, ProcessRecord],
    ) -> dict[str, Any]:
        key = self._identity(record.pid, record.created_at)
        now = time.monotonic()
        cached = self._metadata_cache.get(key)
        if cached:
            cached_at, value = cached
            if now - cached_at < 30 or not value.get("_has_unavailable"):
                return value

        exe, exe_error = read_field(record.proc.exe)
        argv, cmd_error = read_field(record.proc.cmdline)
        cwd, cwd_error = read_field(record.proc.cwd)
        exe = str(exe) if exe else None
        cwd = str(cwd) if cwd else None
        argv = list(argv) if isinstance(argv, (tuple, list)) else None
        label, script_path, module_name, project_path = script_info(
            argv, record.name, cwd
        )
        parent = records.get(record.ppid) if record.ppid is not None else None
        parent_info = {
            "pid": record.ppid,
            "name": parent.name if parent else ("Unavailable" if record.ppid else None),
            "created_at": parent.created_at if parent else None,
            "cmdline": None,
            "exe": None,
        }
        command = normalize_cmdline(argv)
        if command is None:
            command = cmd_error or "Unavailable"
        executable = exe or exe_error or "Unavailable"
        working_dir = cwd or cwd_error or "Unavailable"
        virtualenv = None
        if exe:
            try:
                exe_path = Path(exe)
                if exe_path.parent.name.lower() == "scripts":
                    candidate = exe_path.parent.parent
                    if (candidate / "pyvenv.cfg").is_file():
                        virtualenv = str(candidate)
            except (OSError, ValueError):
                pass
        value = {
            "pid": record.pid,
            "ppid": record.ppid,
            "process_name": record.name,
            "exe": executable,
            "cmdline": command,
            "cwd": working_dir,
            "script_label": label,
            "script_path": script_path,
            "module_name": module_name,
            "project_path": project_path or working_dir,
            "created_at": record.created_at,
            "parent_process": parent_info,
            "virtualenv_path": virtualenv,
            "exe_status": exe_error,
            "cmdline_status": cmd_error,
            "cwd_status": cwd_error,
            "_has_unavailable": bool(exe_error or cmd_error or cwd_error),
        }
        self._metadata_cache[key] = (now, value)
        return value

    def _measure_process(
        self,
        record: ProcessRecord,
        total_memory_bytes: int,
        now_mono: float,
    ) -> dict[str, Any]:
        key = self._identity(record.pid, record.created_at)
        raw_cpu, cpu_error = read_field(lambda: record.proc.cpu_percent(None))
        cpu_pct = None
        if raw_cpu is not None:
            try:
                raw_cpu = float(raw_cpu)
                if key not in self._cpu_primed:
                    self._cpu_primed.add(key)
                else:
                    cpu_pct = max(0.0, raw_cpu / self.cpu_count)
            except (TypeError, ValueError):
                cpu_error = "Unavailable"

        memory, memory_error = read_field(record.proc.memory_info)
        rss_mb = None
        if memory is not None:
            try:
                rss_mb = float(memory.rss / (1024 ** 2))
            except (AttributeError, TypeError, ValueError):
                memory_error = "Unavailable"
        mem_pct = (
            float(memory.rss / total_memory_bytes * 100.0)
            if memory is not None and total_memory_bytes > 0
            else None
        )

        threads, threads_error = read_field(record.proc.num_threads)
        try:
            threads = int(threads) if threads is not None else None
        except (TypeError, ValueError):
            threads = None
            threads_error = "Unavailable"

        io, io_error = read_field(record.proc.io_counters)
        read_mb_s = write_mb_s = None
        if io is not None:
            try:
                previous = self._io_baselines.get(key)
                if previous:
                    before_t, before_read, before_write = previous
                    dt = max(0.001, now_mono - before_t)
                    read_mb_s = max(0.0, (io.read_bytes - before_read) / dt / (1024 ** 2))
                    write_mb_s = max(0.0, (io.write_bytes - before_write) / dt / (1024 ** 2))
                self._io_baselines[key] = (
                    now_mono, int(io.read_bytes), int(io.write_bytes)
                )
            except (AttributeError, TypeError, ValueError):
                io_error = "Unavailable"

        return {
            "cpu_pct": cpu_pct,
            "cpu_status": cpu_error if cpu_pct is None else None,
            "rss_mb": rss_mb,
            "mem_pct": mem_pct,
            "memory_status": memory_error if rss_mb is None else None,
            "threads": threads,
            "threads_status": threads_error if threads is None else None,
            "read_mb_s": read_mb_s,
            "write_mb_s": write_mb_s,
            "io_status": io_error if io is None else None,
        }

    @staticmethod
    def _sum_known(values: list[float | None], *, empty_zero: bool = False):
        known = [float(value) for value in values if value is not None]
        if known:
            return sum(known)
        return 0.0 if empty_zero and not values else None

    @staticmethod
    def _adopt_pending(state: ProcState, pending: dict[str, Any]) -> None:
        count = int(pending.get("sample_count") or 0)
        state.session_id = int(pending["id"])
        state.sample_count += count
        state.sum_cpu += float(pending.get("avg_cpu_pct") or 0.0) * count
        state.sum_rss += float(pending.get("avg_rss_mb") or 0.0) * count
        state.max_cpu = max(state.max_cpu, float(pending.get("max_cpu_pct") or 0.0))
        state.max_rss = max(state.max_rss, float(pending.get("max_rss_mb") or 0.0))
        state.peak_mem_pct = max(
            state.peak_mem_pct, float(pending.get("peak_mem_pct") or 0.0)
        )
        old_health = pending.get("health_worst") or "HEALTHY"
        state.health_worst = worst(state.health_worst, old_health)
        try:
            raw_members = json.loads(pending.get("tree_members_json") or "[]")
            if isinstance(raw_members, dict):
                state.tree_members_complete = raw_members.get("complete") is True
                raw_items = raw_members.get("members", [])
            else:
                # A list from an older build only described the latest tree,
                # not every still-live descendant previously observed.
                state.tree_members_complete = False
                raw_items = raw_members
            state.tree_members = [
                (int(item[0]), float(item[1]) if item[1] is not None else None)
                for item in raw_items
                if isinstance(item, (list, tuple)) and len(item) == 2
            ]
        except (json.JSONDecodeError, TypeError, ValueError):
            state.tree_members = []
            state.tree_members_complete = False

    def _restore_state(
        self,
        record: ProcessRecord,
        meta: dict[str, Any],
        pending: dict[str, Any] | None,
        now: float,
        storage,
    ) -> ProcState:
        state = ProcState(
            proc=record.proc,
            key=self._identity(record.pid, record.created_at),
            session_id=None,
            meta=meta,
            first_seen=float((pending or {}).get("first_seen_at") or now),
            last_seen=now,
        )
        if pending:
            self._adopt_pending(state, pending)
            state.monitor_interrupted = True
        elif record.created_at is not None:
            state.session_id = self._db_call(
                storage,
                "create_session",
                history_metadata(
                    {**meta, "first_seen_at": now, "last_seen_at": now},
                    include_private_details=bool(
                        self.cfg.get("persist_python_metadata", False)
                    ),
                ),
                default=None,
            )
        return state

    def _ensure_session(self, state: ProcState, storage, now: float) -> None:
        if state.session_id is not None or state.key[1] is None:
            return
        state.session_id = self._db_call(
            storage,
            "create_session",
            {
                **history_metadata(
                    {
                        **state.meta,
                        "first_seen_at": state.first_seen,
                        "last_seen_at": now,
                    },
                    include_private_details=bool(
                        self.cfg.get("persist_python_metadata", False)
                    ),
                ),
            },
            default=None,
        )

    def _persist_summary(
        self,
        storage,
        state: ProcState,
        now: float,
        progress: tuple[float | None, str | None, str | None] = (None, None, None),
    ) -> None:
        if state.session_id is None:
            return
        pct, phase, message = progress
        self._db_call(
            storage,
            "touch_session",
            state.session_id,
            last_seen_at=now,
            max_cpu_pct=state.max_cpu,
            avg_cpu_pct=state.sum_cpu / max(1, state.sample_count),
            max_rss_mb=state.max_rss,
            avg_rss_mb=state.sum_rss / max(1, state.sample_count),
            peak_mem_pct=state.peak_mem_pct,
            health_worst=state.health_worst,
            sample_count=state.sample_count,
            progress_pct=pct,
            progress_phase=phase,
            progress_message=message,
            tree_members_json=(
                json.dumps(
                    {
                        "members": state.tree_members,
                        "complete": state.tree_members_complete,
                    },
                    separators=(",", ":"),
                )
                if self.cfg.get("persist_python_metadata", False)
                else None
            ),
            default=None,
        )

    def _scan(self, storage):
        now = time.time()
        now_mono = time.monotonic()
        host_cpu_pct = float(psutil.cpu_percent(None))
        sample_metrics = self._universal_sample_tick % 2 == 0
        self._universal_sample_tick += 1
        all_records = self._process_snapshot(
            include_internal=True,
            sample_metrics=sample_metrics,
        )
        try:
            inventory_records = {
                pid: InventoryProcessRecord(
                    proc=record.proc,
                    pid=record.pid,
                    ppid=record.ppid,
                    name=record.name,
                    created_at=record.created_at,
                    measurements=record.readings,
                    sample_metrics=record.sample_metrics,
                )
                for pid, record in all_records.items()
            }
            process_snapshot = self._process_inventory.collect(
                inventory_records,
                sampled_at=now,
                monotonic_at=now_mono,
                enumeration_complete=self._enumeration_complete,
                enumeration_permission_denied=self._enumeration_permission_denied,
            )
            self.process_snapshot_ready.emit(process_snapshot)
        except Exception as exc:
            self.monitor_error.emit(
                f"Universal process sample: {type(exc).__name__}: {exc}"
            )

        records = {
            pid: record for pid, record in all_records.items()
            if pid != self._self_pid
            and not (
                (created := self._self_launcher_identities.get(pid)) is not None
                and record.created_at is not None
                and abs(created - record.created_at) <= 0.01
            )
        }
        python_pids = {
            pid for pid, record in records.items() if self._watch_name(record.name)
        }
        children = self._children_map(records)

        root_for: dict[int, int] = {}
        nearest_parent: dict[int, int | None] = {}
        ancestry_uncertain: dict[int, bool] = {}
        for pid in python_pids:
            root_pid, parent_pid, uncertain = self._python_ancestry(
                records[pid], records, python_pids
            )
            root_for[pid] = root_pid
            nearest_parent[pid] = parent_pid
            ancestry_uncertain[pid] = uncertain
        root_pids = sorted(pid for pid in python_pids if root_for[pid] == pid)

        trees: dict[int, list[tuple[int, int]]] = {}
        relevant_pids: set[int] = set()
        for root_pid in root_pids:
            tree = self._subtree(root_pid, records, children)
            trees[root_pid] = tree
            relevant_pids.update(pid for pid, _ in tree)

        sys_vm = psutil.virtual_memory()
        total_memory = int(sys_vm.total)
        metrics: dict[int, dict[str, Any]] = {}
        relevant_keys: set[Identity] = set()
        for pid in sorted(relevant_pids):
            record = records[pid]
            key = self._identity(pid, record.created_at)
            relevant_keys.add(key)
            metrics[pid] = self._measure_process(record, total_memory, now_mono)

        self._io_baselines = {
            key: value for key, value in self._io_baselines.items()
            if key in relevant_keys
        }
        self._cpu_primed.intersection_update(relevant_keys)
        self._metadata_cache = {
            key: value for key, value in self._metadata_cache.items()
            if key in relevant_keys or key in self.states
        }

        active_keys: set[Identity] = set()
        state_rows: list[tuple[ProcState, list[tuple[int, int]], dict[str, Any]]] = []
        matched_pending: set[Identity] = set()
        root_meta: dict[int, dict[str, Any]] = {}
        for root_pid in root_pids:
            record = records[root_pid]
            key = self._identity(record.pid, record.created_at)
            meta = self._static_metadata(record, records)
            meta["python_parent_pid"] = nearest_parent.get(root_pid)
            meta["root_workload_pid"] = root_pid
            meta["root_confidence"] = (
                "parent relationship incomplete"
                if ancestry_uncertain.get(root_pid)
                else "confirmed from process snapshot"
            )
            root_meta[root_pid] = meta

            pending = self._pending_by_key.pop(key, None)
            if pending:
                matched_pending.add(key)
            state = self.states.get(key)
            if state is None:
                state = self._restore_state(record, meta, pending, now, storage)
                self.states[key] = state
            else:
                state.proc = record.proc
                state.meta = meta
                if pending:
                    self._adopt_pending(state, pending)
                self._ensure_session(state, storage, now)
            state.last_seen = now
            active_keys.add(key)
            state_rows.append((state, trees[root_pid], meta))

        # Old versions tracked nested Python processes as independent sessions.
        # Close those observations as reclassified, without claiming exit.
        for pid in sorted(python_pids - set(root_pids)):
            record = records[pid]
            key = self._identity(pid, record.created_at)
            pending = self._pending_by_key.pop(key, None)
            if pending:
                matched_pending.add(key)
                self._db_call(
                    storage,
                    "end_observation",
                    int(pending["id"]),
                    now,
                    "process_running_as_nested_workload",
                    default=None,
                )

        if not self._pending_loaded and storage is not None:
            self._load_pending(storage)
        if self._pending_loaded:
            for key, pending in list(self._pending_by_key.items()):
                if key in matched_pending:
                    continue
                current = records.get(key[0])
                if current is None or (
                    current.created_at is not None and current.created_at != key[1]
                ):
                    self._db_call(
                        storage, "end_while_offline", int(pending["id"]), now,
                        default=None,
                    )
                    self._pending_by_key.pop(key, None)
                elif current.created_at is not None and not self._watch_name(current.name):
                    self._db_call(
                        storage, "end_while_offline", int(pending["id"]), now,
                        default=None,
                    )
                    self._pending_by_key.pop(key, None)
                # Same PID with unavailable creation time is ambiguous. Keep the
                # observation open rather than inventing an end or a reattachment.

        ended = [key for key in self.states if key not in active_keys]
        for key in ended:
            state = self.states.pop(key)
            current = records.get(key[0])
            same_identity = (
                current is not None
                and (
                    key[1] is None
                    or current.created_at is None
                    or current.created_at == key[1]
                )
            )
            if same_identity:
                self._db_call(
                    storage,
                    "end_observation",
                    state.session_id,
                    now,
                    "process_running_as_nested_workload",
                    default=None,
                )
            else:
                self._finalize(storage, state, now, "process_ended", records)

        active_rows: list[dict[str, Any]] = []
        notify_events: list[tuple[str, str]] = []
        for state, tree, meta in state_rows:
            row, events = self._build_row(
                storage,
                state,
                tree,
                records,
                python_pids,
                children,
                metrics,
                meta,
                now,
                total_memory,
                host_cpu_pct,
                float(sys_vm.percent),
            )
            active_rows.append(row)
            notify_events.extend(events)

        workload_cpu = self._sum_known(
            [row["tree_cpu_pct"] for row in active_rows], empty_zero=True
        )
        workload_ram = self._sum_known(
            [row["tree_rss_mb"] for row in active_rows], empty_zero=True
        )
        system = {
            "cpu_pct": host_cpu_pct,
            "ram_pct": float(sys_vm.percent),
            "ram_used_gb": float((sys_vm.total - sys_vm.available) / (1024 ** 3)),
            "ram_total_gb": float(sys_vm.total / (1024 ** 3)),
            "python_rss_mb": workload_ram,
            "python_cpu_pct": workload_cpu,
            "python_count": len(active_rows),
            "python_process_count": len(python_pids),
            "paused": self._paused,
        }
        active_rows.sort(
            key=lambda row: (
                SEVERITY_ORDER.get(row["health"], 0),
                row["tree_cpu_pct"] or 0.0,
                row["tree_rss_mb"] or 0.0,
            ),
            reverse=True,
        )
        self.snapshot.emit(active_rows, system)
        for severity, message in notify_events:
            self.alert.emit(severity, message)

    def _record_tree_members(
        self,
        state: ProcState,
        tree: list[tuple[int, int]],
        records: dict[int, ProcessRecord],
    ) -> None:
        """Keep identities of every previously observed descendant still alive."""
        current_members: set[Identity] = set()
        complete = state.tree_members_complete
        for pid, _depth in tree:
            record = records.get(pid)
            if record is None:
                complete = False
                continue
            identity = (pid, record.created_at)
            current_members.add(identity)
            if record.created_at is None:
                complete = False

        retained = set(current_members)
        if complete:
            for pid, created_at in state.tree_members:
                identity = (pid, created_at)
                if identity in current_members:
                    continue
                current = records.get(pid)
                if current is not None:
                    if created_at is None or current.created_at is None:
                        complete = False
                        retained.add(identity)
                    elif abs(current.created_at - created_at) <= 0.01:
                        # The child is still alive, even if it detached from the
                        # root and is no longer a member of the current tree.
                        retained.add(identity)
                    # A different create time proves the previous identity ended.
                    continue

                try:
                    actual_created = psutil.Process(pid).create_time()
                except psutil.NoSuchProcess:
                    continue
                except (psutil.AccessDenied, psutil.Error, OSError, ProcessLookupError):
                    complete = False
                    retained.add(identity)
                    continue
                if created_at is None:
                    complete = False
                    retained.add(identity)
                elif abs(float(actual_created) - created_at) <= 0.01:
                    retained.add(identity)

        else:
            # Once any identity is ambiguous, keep the bounded evidence but do
            # not later turn it into a clean lifecycle claim.
            retained.update(state.tree_members)

        if len(retained) > MAX_TRACKED_TREE_IDENTITIES:
            complete = False
            retained = set(sorted(
                retained,
                key=lambda item: (item[0], item[1] if item[1] is not None else -1.0),
            )[:MAX_TRACKED_TREE_IDENTITIES])
        state.tree_members = sorted(
            retained,
            key=lambda item: (item[0], item[1] if item[1] is not None else -1.0),
        )
        state.tree_members_complete = complete

    def _build_row(
        self,
        storage,
        state: ProcState,
        tree: list[tuple[int, int]],
        records: dict[int, ProcessRecord],
        python_pids: set[int],
        children: dict[int, list[int]],
        metrics: dict[int, dict[str, Any]],
        meta: dict[str, Any],
        now: float,
        total_memory: int,
        host_cpu_pct: float,
        host_ram_pct: float,
    ):
        root_pid = state.key[0]
        pids = [pid for pid, _ in tree]
        descendants = pids[1:]
        own = metrics.get(root_pid, {})
        child_cpu_values = [metrics.get(pid, {}).get("cpu_pct") for pid in descendants]
        child_rss_values = [metrics.get(pid, {}).get("rss_mb") for pid in descendants]
        all_cpu_values = [own.get("cpu_pct")] + child_cpu_values
        all_rss_values = [own.get("rss_mb")] + child_rss_values
        children_cpu = self._sum_known(child_cpu_values, empty_zero=True)
        children_rss = self._sum_known(child_rss_values, empty_zero=True)
        tree_cpu = self._sum_known(all_cpu_values)
        tree_rss = self._sum_known(all_rss_values)
        cpu_unknown = sum(value is None for value in all_cpu_values)
        ram_unknown = sum(value is None for value in all_rss_values)
        self._record_tree_members(state, tree, records)

        # Post-order totals for each tree node; each process identity occurs
        # once in a tree, so every descendant contributes exactly once.
        tree_pids = set(pids)
        subtree_aggregates: dict[int, dict[str, float | None]] = {}
        for pid, _depth in reversed(tree):
            own_metric = metrics.get(pid, {})
            cpu_values = [own_metric.get("cpu_pct")]
            rss_values = [own_metric.get("rss_mb")]
            for child in children.get(pid, []):
                if child in tree_pids and child in subtree_aggregates:
                    cpu_values.append(subtree_aggregates[child]["cpu_pct"])
                    rss_values.append(subtree_aggregates[child]["rss_mb"])
            subtree_aggregates[pid] = {
                "cpu_pct": self._sum_known(cpu_values),
                "rss_mb": self._sum_known(rss_values),
            }

        tree_rows = []
        for pid, depth in tree:
            record = records[pid]
            metric = metrics.get(pid, {})
            subtree = subtree_aggregates.get(pid, {})
            nested_parent = None
            if pid in python_pids and pid != root_pid:
                nested_parent = self._python_ancestry(
                    record, records, python_pids
                )[1]
            tree_rows.append({
                "pid": pid,
                "ppid": record.ppid,
                "name": record.name,
                "created_at": record.created_at,
                "snapshot_at": now,
                "depth": depth,
                "is_python": pid in python_pids,
                "python_parent_pid": nested_parent,
                "cpu_pct": metric.get("cpu_pct"),
                "rss_mb": metric.get("rss_mb"),
                "mem_pct": metric.get("mem_pct"),
                "threads": metric.get("threads"),
                "read_mb_s": metric.get("read_mb_s"),
                "write_mb_s": metric.get("write_mb_s"),
                "cpu_status": metric.get("cpu_status"),
                "memory_status": metric.get("memory_status"),
                "subtree_cpu_pct": subtree.get("cpu_pct"),
                "subtree_rss_mb": subtree.get("rss_mb"),
                "exe": None,
                "cmdline": None,
                "cwd": None,
            })

        started_at = state.key[1]
        elapsed = max(0.0, now - started_at) if started_at is not None else None
        progress = self._read_heartbeat(root_pid)
        health, reasons = self._health(state, now, tree_cpu, tree_rss)
        state.health_worst = worst(state.health_worst, health)

        if tree_cpu is not None:
            state.max_cpu = max(state.max_cpu, tree_cpu)
        if tree_rss is not None:
            state.max_rss = max(state.max_rss, tree_rss)
            state.peak_mem_pct = max(
                state.peak_mem_pct,
                tree_rss / max(1, total_memory) * 100.0,
            )
            state.mem_history.append((now, tree_rss))
        if tree_cpu is not None and tree_rss is not None:
            state.sample_count += 1
            state.sum_cpu += tree_cpu
            state.sum_rss += tree_rss

        db_interval = float(self.cfg["db_sample_interval_sec"])
        if state.session_id is not None and now - state.last_db_sample >= db_interval:
            self._db_call(
                storage, "add_sample",
                state.session_id, now, tree_cpu, tree_rss,
                (tree_rss / total_memory * 100.0 if tree_rss is not None else None),
                own.get("threads"), own.get("read_mb_s"), own.get("write_mb_s"),
                host_cpu_pct,
                host_ram_pct,
                cpu_unknown,
                ram_unknown,
                len(descendants),
                default=None,
            )
            self._persist_summary(storage, state, now, progress)
            state.last_db_sample = now

        events: list[tuple[str, str]] = []
        if health in {"HIGH", "CRITICAL"} and reasons:
            cooldown = float(self.cfg["notification_cooldown_sec"])
            last = state.alert_last_at.get(health, 0.0)
            if now - last >= cooldown:
                message = f"{meta['script_label']} (PID {root_pid}) — " + "; ".join(reasons)
                if state.session_id is not None:
                    self._db_call(
                        storage, "add_alert",
                        state.session_id, now, health, "workload",
                        max(tree_cpu or 0.0, tree_rss or 0.0), message,
                        default=None,
                    )
                state.alert_last_at[health] = now
                events.append((health, message))

        row = {
            **meta,
            "pid": root_pid,
            "started_at": started_at,
            "elapsed_s": elapsed,
            "own_cpu_pct": own.get("cpu_pct"),
            "own_rss_mb": own.get("rss_mb"),
            "own_mem_pct": own.get("mem_pct"),
            "own_threads": own.get("threads"),
            "own_read_mb_s": own.get("read_mb_s"),
            "own_write_mb_s": own.get("write_mb_s"),
            "children_cpu_pct": children_cpu,
            "children_rss_mb": children_rss,
            "tree_cpu_pct": tree_cpu,
            "tree_rss_mb": tree_rss,
            "tree_cpu_unknown": cpu_unknown,
            "tree_ram_unknown": ram_unknown,
            "child_count": len(descendants),
            "process_tree": tree_rows,
            "health": health,
            "health_reasons": reasons,
            "progress_pct": progress[0],
            "progress_phase": progress[1],
            "progress_message": progress[2],
            "session_id": state.session_id,
        }
        return row, events

    def _health(
        self,
        state: ProcState,
        now: float,
        cpu_pct: float | None,
        rss_mb: float | None,
    ):
        hcfg = self.cfg["health"]
        severity = "HEALTHY"
        reasons: list[str] = []
        thresholds = [
            ("WATCH", float(hcfg["cpu_watch_pct"]),
             float(hcfg.get("cpu_watch_sustain_sec", 5.0))),
            ("HIGH", float(hcfg["cpu_high_pct"]),
             float(hcfg.get("cpu_high_sustain_sec", 8.0))),
            ("CRITICAL", float(hcfg["cpu_critical_pct"]),
             float(hcfg["cpu_sustain_sec"])),
        ]
        if cpu_pct is None:
            state.cpu_since.clear()
        else:
            for name, threshold, sustain in thresholds:
                if cpu_pct >= threshold:
                    state.cpu_since.setdefault(name, now)
                    if now - state.cpu_since[name] >= sustain:
                        severity = worst(severity, name)
                        reasons.append(f"CPU {cpu_pct:.0f}% sustained")
                else:
                    state.cpu_since.pop(name, None)

        if rss_mb is not None:
            if rss_mb >= float(hcfg["ram_critical_mb"]):
                severity = worst(severity, "CRITICAL")
                reasons.append(f"RAM {rss_mb / 1024:.1f} GB")
            elif rss_mb >= float(hcfg["ram_high_mb"]):
                severity = worst(severity, "HIGH")
                reasons.append(f"RAM {rss_mb / 1024:.1f} GB")
            elif rss_mb >= float(hcfg["ram_watch_mb"]):
                severity = worst(severity, "WATCH")
                reasons.append(f"RAM {rss_mb:.0f} MB")

            window = [
                (ts, value)
                for ts, value in state.mem_history
                if ts >= now - 60.0
            ]
            if window and now - window[0][0] >= 45.0:
                elapsed = max(1.0, now - window[0][0])
                growth_per_min = (rss_mb - window[0][1]) / elapsed * 60.0
                if growth_per_min >= float(hcfg["memory_growth_high_mb_per_min"]):
                    severity = worst(severity, "HIGH")
                    reasons.append(f"RAM +{growth_per_min:.0f} MB/min")
        return severity, reasons

    def _read_heartbeat(self, pid: int):
        path = HEARTBEAT_DIR / f"{pid}.json"
        if not path.exists():
            return None, None, None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(raw.get("ts", 0)) > 30:
                return None, None, None
            progress = raw.get("progress_pct")
            if progress is not None:
                progress = max(0.0, min(100.0, float(progress)))
            return progress, raw.get("phase"), raw.get("message")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None, None, None

    def _tree_end_state(
        self,
        state: ProcState,
        records: dict[int, ProcessRecord],
    ) -> tuple[str, str]:
        if not state.tree_members_complete:
            return (
                "unknown",
                "Previously observed process-tree identities could not all be tracked; final descendant cleanup cannot be confirmed.",
            )
        if not state.tree_members:
            return "unknown", "No persisted process-tree identity snapshot is available."

        residual: list[int] = []
        unknown: list[int] = []
        for pid, created_at in state.tree_members:
            if pid == state.key[0] and created_at == state.key[1]:
                continue
            current = records.get(pid)
            if current is not None:
                if created_at is None or current.created_at is None:
                    unknown.append(pid)
                elif abs(current.created_at - created_at) <= 0.01:
                    residual.append(pid)
                # A different creation time proves that the old PID identity ended.
                continue
            try:
                actual_created = psutil.Process(pid).create_time()
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.Error, OSError, ProcessLookupError):
                unknown.append(pid)
                continue
            if created_at is None:
                unknown.append(pid)
            elif abs(float(actual_created) - created_at) <= 0.01:
                residual.append(pid)

        if unknown:
            return (
                "unknown",
                f"Could not verify the final state of {len(unknown)} previously observed descendant(s).",
            )
        if residual:
            return (
                "residual",
                f"{len(residual)} previously observed descendant(s) remained after the root ended; their expected lifetime is unknown.",
            )
        descendants = max(0, len(state.tree_members) - 1)
        return (
            "cleared",
            f"The root ended and all {descendants} previously observed descendant(s) were gone or had a different process identity.",
        )

    def _finalize(
        self,
        storage,
        state: ProcState,
        ended_at: float,
        reason: str,
        records: dict[int, ProcessRecord],
    ):
        if state.session_id is None or state.key[1] is None:
            return
        tree_end_state, tree_end_detail = self._tree_end_state(state, records)
        session = self._db_call(
            storage, "session_for_scoring", state.session_id, default=None
        ) or {}
        session.update({
            "created_at": state.key[1],
            "first_seen_at": state.first_seen,
            "ended_at": ended_at,
            "duration_s": max(0.0, ended_at - state.key[1]),
            "end_reason": reason,
            "max_cpu_pct": state.max_cpu,
            "avg_cpu_pct": state.sum_cpu / max(1, state.sample_count),
            "max_rss_mb": state.max_rss,
            "avg_rss_mb": state.sum_rss / max(1, state.sample_count),
            "peak_mem_pct": state.peak_mem_pct,
            "sample_count": state.sample_count,
            "monitor_interrupted": state.monitor_interrupted,
            "expected_sample_interval_s": self.cfg.get(
                "db_sample_interval_sec", 10.0
            ),
        })
        samples = self._db_call(
            storage, "samples_for_session", state.session_id, default=[]
        )
        if samples is None:
            samples = ()
        try:
            run_health = score_run_health(
                session,
                samples,
                tree_end_state=tree_end_state,
                tree_end_detail=tree_end_detail,
            )
        except (sqlite3.Error, OSError) as exc:
            close_samples = getattr(samples, "close", None)
            if close_samples:
                close_samples()
            self.monitor_error.emit(
                f"SQLite samples_for_session: {type(exc).__name__}: {exc}"
            )
            run_health = score_run_health(
                session,
                (),
                tree_end_state="unknown",
                tree_end_detail="Stored sample data could not be read during finalization.",
            )
        self._db_call(
            storage,
            "end_session",
            state.session_id,
            ended_at=ended_at,
            started_at=state.key[1],
            end_reason=reason,
            max_cpu_pct=state.max_cpu,
            avg_cpu_pct=state.sum_cpu / max(1, state.sample_count),
            max_rss_mb=state.max_rss,
            avg_rss_mb=state.sum_rss / max(1, state.sample_count),
            peak_mem_pct=state.peak_mem_pct,
            health_worst=state.health_worst,
            sample_count=state.sample_count,
            health_score_json=json.dumps(
                run_health, separators=(",", ":"), ensure_ascii=False
            ),
            tree_end_state=tree_end_state,
            tree_end_detail=tree_end_detail,
            default=None,
        )
