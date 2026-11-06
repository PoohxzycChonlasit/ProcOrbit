from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid INTEGER NOT NULL,
    ppid INTEGER,
    process_name TEXT,
    exe TEXT,
    cmdline TEXT,
    cwd TEXT,
    script_label TEXT,
    created_at REAL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    ended_at REAL,
    duration_s REAL,
    end_reason TEXT,
    max_cpu_pct REAL DEFAULT 0,
    avg_cpu_pct REAL DEFAULT 0,
    max_rss_mb REAL DEFAULT 0,
    avg_rss_mb REAL DEFAULT 0,
    peak_mem_pct REAL DEFAULT 0,
    health_worst TEXT DEFAULT 'HEALTHY',
    sample_count INTEGER DEFAULT 0,
    progress_pct REAL,
    progress_phase TEXT,
    progress_message TEXT,
    script_path TEXT,
    module_name TEXT,
    project_path TEXT,
    observation_state TEXT NOT NULL DEFAULT 'open',
    observation_ended_at REAL,
    tree_members_json TEXT,
    tree_end_state TEXT,
    tree_end_detail TEXT,
    health_score_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_ended_at ON sessions(ended_at);
CREATE INDEX IF NOT EXISTS idx_sessions_pid ON sessions(pid);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    cpu_pct REAL,
    rss_mb REAL,
    mem_pct REAL,
    threads INTEGER,
    read_mb_s REAL,
    write_mb_s REAL,
    host_cpu_pct REAL,
    host_ram_pct REAL,
    tree_unknown_count INTEGER,
    tree_cpu_unknown_count INTEGER,
    tree_ram_unknown_count INTEGER,
    child_count INTEGER,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_samples_session_ts ON samples(session_id, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    severity TEXT NOT NULL,
    metric TEXT,
    value REAL,
    message TEXT,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_alerts_session_ts ON alerts(session_id, ts);
"""


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._migrate_schema()
            self.conn.execute("PRAGMA user_version=3")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _migrate_schema(self) -> None:
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        additions = {
            "script_path": "TEXT",
            "module_name": "TEXT",
            "project_path": "TEXT",
            "observation_state": "TEXT NOT NULL DEFAULT 'open'",
            "observation_ended_at": "REAL",
            "tree_members_json": "TEXT",
            "tree_end_state": "TEXT",
            "tree_end_detail": "TEXT",
            "health_score_json": "TEXT",
        }
        for column, declaration in additions.items():
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {column} {declaration}"
                )
        sample_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(samples)").fetchall()
        }
        for column, declaration in {
            "host_cpu_pct": "REAL",
            "host_ram_pct": "REAL",
            "tree_unknown_count": "INTEGER",
            "tree_cpu_unknown_count": "INTEGER",
            "tree_ram_unknown_count": "INTEGER",
            "child_count": "INTEGER",
        }.items():
            if column not in sample_columns:
                self.conn.execute(
                    f"ALTER TABLE samples ADD COLUMN {column} {declaration}"
                )
        # Older releases used ended_at as both the process end and observation end.
        self.conn.execute(
            """
            UPDATE sessions
            SET observation_state='closed',
                observation_ended_at=COALESCE(observation_ended_at, ended_at)
            WHERE ended_at IS NOT NULL AND observation_state='open'
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_observation "
            "ON sessions(observation_state, observation_ended_at)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_history_page "
            "ON sessions(observation_state, "
            "COALESCE(observation_ended_at, ended_at, 0) DESC, id DESC)"
        )

    def close(self) -> None:
        self.conn.close()

    def open_sessions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE observation_state='open'"
        ).fetchall()
        return [dict(row) for row in rows]

    def create_session(self, meta: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO sessions (
                pid, ppid, process_name, exe, cmdline, cwd, script_label,
                created_at, first_seen_at, last_seen_at, script_path,
                module_name, project_path, observation_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                meta.get("pid"),
                meta.get("ppid"),
                meta.get("process_name"),
                meta.get("exe"),
                meta.get("cmdline"),
                meta.get("cwd"),
                meta.get("script_label"),
                meta.get("created_at"),
                meta.get("first_seen_at"),
                meta.get("last_seen_at"),
                meta.get("script_path"),
                meta.get("module_name"),
                meta.get("project_path"),
            ),
        )
        return int(cur.lastrowid)

    def touch_session(
        self,
        session_id: int,
        *,
        last_seen_at: float,
        max_cpu_pct: float,
        avg_cpu_pct: float,
        max_rss_mb: float,
        avg_rss_mb: float,
        peak_mem_pct: float,
        health_worst: str,
        sample_count: int,
        progress_pct: float | None,
        progress_phase: str | None,
        progress_message: str | None,
        tree_members_json: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE sessions
            SET last_seen_at=?, max_cpu_pct=?, avg_cpu_pct=?,
                max_rss_mb=?, avg_rss_mb=?, peak_mem_pct=?,
                health_worst=?, sample_count=?,
                progress_pct=?, progress_phase=?, progress_message=?,
                tree_members_json=?
            WHERE id=? AND observation_state='open'
            """,
            (
                last_seen_at,
                max_cpu_pct,
                avg_cpu_pct,
                max_rss_mb,
                avg_rss_mb,
                peak_mem_pct,
                health_worst,
                sample_count,
                progress_pct,
                progress_phase,
                progress_message,
                tree_members_json,
                session_id,
            ),
        )

    def add_sample(
        self,
        session_id: int,
        ts: float,
        cpu_pct: float | None,
        rss_mb: float | None,
        mem_pct: float | None,
        threads: int | None,
        read_mb_s: float | None,
        write_mb_s: float | None,
        host_cpu_pct: float | None,
        host_ram_pct: float | None,
        tree_cpu_unknown_count: int,
        tree_ram_unknown_count: int,
        child_count: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO samples (
                session_id, ts, cpu_pct, rss_mb, mem_pct, threads, read_mb_s,
                write_mb_s, host_cpu_pct, host_ram_pct, tree_unknown_count,
                tree_cpu_unknown_count, tree_ram_unknown_count, child_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, ts, cpu_pct, rss_mb, mem_pct, threads, read_mb_s,
                write_mb_s, host_cpu_pct, host_ram_pct,
                int(tree_cpu_unknown_count + tree_ram_unknown_count),
                tree_cpu_unknown_count, tree_ram_unknown_count, child_count,
            ),
        )

    def add_alert(
        self,
        session_id: int,
        ts: float,
        severity: str,
        metric: str,
        value: float,
        message: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO alerts (session_id, ts, severity, metric, value, message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, ts, severity, metric, value, message),
        )

    def end_session(
        self,
        session_id: int,
        *,
        ended_at: float,
        started_at: float,
        end_reason: str,
        max_cpu_pct: float,
        avg_cpu_pct: float,
        max_rss_mb: float,
        avg_rss_mb: float,
        peak_mem_pct: float,
        health_worst: str,
        sample_count: int,
        health_score_json: str,
        tree_end_state: str,
        tree_end_detail: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE sessions
            SET ended_at=?, observation_ended_at=?, observation_state='closed',
                duration_s=?, end_reason=?, max_cpu_pct=?, avg_cpu_pct=?,
                max_rss_mb=?, avg_rss_mb=?, peak_mem_pct=?, health_worst=?,
                sample_count=?, health_score_json=?, tree_end_state=?,
                tree_end_detail=?
            WHERE id=?
            """,
            (
                ended_at,
                ended_at,
                max(0.0, ended_at - started_at),
                end_reason,
                max_cpu_pct,
                avg_cpu_pct,
                max_rss_mb,
                avg_rss_mb,
                peak_mem_pct,
                health_worst,
                sample_count,
                health_score_json,
                tree_end_state,
                tree_end_detail,
                session_id,
            ),
        )

    def end_while_offline(self, session_id: int, observed_at: float) -> None:
        # The process exit time is unknowable; preserve the last observation only.
        self.conn.execute(
            """
            UPDATE sessions
            SET observation_state='closed', observation_ended_at=?,
                ended_at=NULL, duration_s=NULL, end_reason='ended_while_monitor_off',
                tree_end_state='unknown',
                tree_end_detail='The monitor was offline when the process ended.'
            WHERE id=? AND observation_state='open'
            """,
            (observed_at, session_id),
        )

    def end_observation(
        self, session_id: int, observed_at: float, reason: str
    ) -> None:
        # Stop tracking without claiming the observed process exited.
        self.conn.execute(
            """
            UPDATE sessions
            SET observation_state='closed', observation_ended_at=?,
                ended_at=NULL, duration_s=NULL, end_reason=?,
                tree_end_state='unknown',
                tree_end_detail='The workload observation ended before root-process termination was confirmed.'
            WHERE id=? AND observation_state='open'
            """,
            (observed_at, reason, session_id),
        )

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        target = max(1, min(1000, int(limit)))
        page = self.history_page(limit=min(100, target))
        rows = list(page["rows"])
        while page["has_more"] and len(rows) < target:
            page = self.history_page(
                cursor=page["next_cursor"],
                limit=min(100, target - len(rows)),
            )
            rows.extend(page["rows"])
        return rows[:target]

    def history_page(
        self,
        *,
        cursor: tuple[float, int] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        page_size = max(1, min(100, int(limit)))
        sort_key = "COALESCE(observation_ended_at, ended_at, 0)"
        if cursor is None:
            rows = self.conn.execute(
                f"""
                SELECT *, {sort_key} AS history_sort_ts
                FROM sessions
                WHERE observation_state='closed'
                ORDER BY history_sort_ts DESC, id DESC
                LIMIT ?
                """,
                (page_size + 1,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"""
                SELECT *, {sort_key} AS history_sort_ts
                FROM sessions
                WHERE observation_state='closed'
                  AND ({sort_key} < ? OR ({sort_key} = ? AND id < ?))
                ORDER BY history_sort_ts DESC, id DESC
                LIMIT ?
                """,
                (float(cursor[0]), float(cursor[0]), int(cursor[1]), page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        items = [dict(row) for row in rows[:page_size]]
        for item in items:
            try:
                item["run_health"] = json.loads(item.get("health_score_json") or "null")
            except (json.JSONDecodeError, TypeError):
                item["run_health"] = None
        next_cursor = None
        if items:
            last = items[-1]
            next_cursor = (
                float(last.get("history_sort_ts") or 0.0),
                int(last["id"]),
            )
        return {"rows": items, "has_more": has_more, "next_cursor": next_cursor}

    def session_for_scoring(self, session_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (int(session_id),)
        ).fetchone()
        return dict(row) if row else None

    def samples_for_session(
        self, session_id: int, *, batch_size: int = 512
    ) -> Iterator[dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT * FROM samples WHERE session_id=? ORDER BY ts, id",
            (int(session_id),),
        )
        bounded_batch_size = max(1, min(2048, int(batch_size)))

        def iterate() -> Iterator[dict[str, Any]]:
            try:
                while True:
                    rows = cursor.fetchmany(bounded_batch_size)
                    if not rows:
                        return
                    for row in rows:
                        yield dict(row)
            finally:
                cursor.close()

        return iterate()

    def cleanup_samples(self, retention_days: int) -> None:
        cutoff = time.time() - max(1, retention_days) * 86400
        self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
