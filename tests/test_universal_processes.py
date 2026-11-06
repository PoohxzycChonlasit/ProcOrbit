from __future__ import annotations

import os
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(APP))

import psutil  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from process_classifier import (  # noqa: E402
    BROWSERS,
    JAVASCRIPT,
    PYTHON,
    UNKNOWN,
    classify_process,
)
from process_identity import ProcessIdentity  # noqa: E402
from process_inventory import (  # noqa: E402
    ProcessInventory,
    ProcessMeasurements,
    ProcessRecord,
    ProcessSample,
    ProcessSnapshot,
)
from process_model import ProcessFilterProxyModel, ProcessTableModel  # noqa: E402
from process_privacy import history_metadata  # noqa: E402
from process_window import ProcessWindow  # noqa: E402


CpuTimes = namedtuple("CpuTimes", "user system")
MemoryInfo = namedtuple("MemoryInfo", "rss")


class FakeProcess:
    def __init__(self, *, cpu=1.0, memory=64 * 1024 * 1024, executable=r"C:\Apps\sample.exe"):
        self.cpu = cpu
        self.memory = memory
        self.executable = executable
        self.cpu_error = None
        self.memory_error = None
        self.exe_error = None

    def cpu_times(self):
        if self.cpu_error:
            raise self.cpu_error
        return CpuTimes(self.cpu, 0.0)

    def memory_info(self):
        if self.memory_error:
            raise self.memory_error
        return MemoryInfo(self.memory)

    def exe(self):
        if self.exe_error:
            raise self.exe_error
        return self.executable


def record(pid=100, *, name="sample.exe", created=100.0, ppid=None, proc=None):
    return ProcessRecord(
        proc=proc or FakeProcess(),
        pid=pid,
        ppid=ppid,
        name=name,
        created_at=created,
    )


def sample(
    pid,
    *,
    created=100.0,
    name="sample.exe",
    category=UNKNOWN,
    cpu=1.0,
    memory=64.0,
    uptime=100.0,
    cpu_sampled_at=None,
    memory_sampled_at=None,
    parent_pid=None,
    parent_name=None,
    executable=None,
    reason="test classification",
):
    return ProcessSample(
        identity=ProcessIdentity(pid, created),
        name=name,
        category=category,
        category_reason=reason,
        cpu_pct=cpu,
        memory_mb=memory,
        uptime_sec=uptime,
        parent_pid=parent_pid,
        parent_name=parent_name,
        executable=executable,
        cpu_sampled_at=cpu_sampled_at,
        memory_sampled_at=memory_sampled_at,
    )


class ProcessIdentityTests(unittest.TestCase):
    def test_creation_time_distinguishes_pid_reuse(self):
        first = ProcessIdentity(100, 10.0)
        reused = ProcessIdentity(100, 20.0)
        self.assertNotEqual(first, reused)
        self.assertTrue(first.verified)

    def test_missing_or_invalid_creation_time_is_unverified(self):
        for value in (None, float("nan"), -1, "invalid"):
            identity = ProcessIdentity(100, value)
            self.assertIsNone(identity.created_at)
            self.assertFalse(identity.verified)


class ProcessInventoryTests(unittest.TestCase):
    def test_process_creation_then_second_sample_has_cpu_rate(self):
        proc = FakeProcess(cpu=2.0)
        inventory = ProcessInventory(cpu_count=2)
        first = inventory.collect(
            {100: record(proc=proc)}, sampled_at=1000, monotonic_at=10
        )
        self.assertEqual(len(first.items), 1)
        self.assertIsNone(first.items[0].cpu_pct)
        self.assertEqual(first.items[0].uptime_sec, 900)

        proc.cpu = 3.0
        second = inventory.collect(
            {100: record(proc=proc)}, sampled_at=1002, monotonic_at=12
        )
        self.assertAlmostEqual(second.items[0].cpu_pct, 25.0)

    def test_pid_reuse_gets_a_new_cpu_baseline(self):
        inventory = ProcessInventory(cpu_count=1)
        inventory.collect(
            {100: record(created=10.0, proc=FakeProcess(cpu=5.0))},
            sampled_at=20,
            monotonic_at=1,
        )
        reused = inventory.collect(
            {100: record(created=30.0, proc=FakeProcess(cpu=9.0))},
            sampled_at=31,
            monotonic_at=2,
        )
        self.assertEqual(reused.items[0].identity, ProcessIdentity(100, 30.0))
        self.assertIsNone(reused.items[0].cpu_pct)

    def test_termination_during_sampling_marks_snapshot_partial(self):
        proc = FakeProcess()
        proc.memory_error = psutil.NoSuchProcess(100)
        result = ProcessInventory().collect(
            {100: record(proc=proc)}, sampled_at=200, monotonic_at=1
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.ended_during_sampling, 1)
        self.assertEqual(result.items, ())

    def test_permission_errors_keep_process_and_report_unknown_fields(self):
        proc = FakeProcess()
        proc.cpu_error = psutil.AccessDenied(100)
        proc.exe_error = psutil.AccessDenied(100)
        result = ProcessInventory().collect(
            {100: record(name="python.exe", proc=proc)},
            sampled_at=200,
            monotonic_at=1,
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.permission_denied, 2)
        self.assertEqual(result.items[0].category, PYTHON)
        self.assertIsNone(result.items[0].cpu_pct)
        self.assertEqual(result.items[0].cpu_status, "Access denied")
        self.assertEqual(result.items[0].executable_status, "Access denied")

    def test_missing_metadata_uses_unknown_category_and_no_fake_uptime(self):
        result = ProcessInventory().collect(
            {100: record(name=None, created=None)},
            sampled_at=200,
            monotonic_at=1,
        )
        self.assertEqual(result.items[0].category, UNKNOWN)
        self.assertIsNone(result.items[0].uptime_sec)
        self.assertIsNone(result.items[0].cpu_pct)

    def test_incomplete_enumeration_does_not_claim_complete_snapshot(self):
        result = ProcessInventory().collect(
            {},
            sampled_at=200,
            monotonic_at=1,
            enumeration_complete=False,
            enumeration_permission_denied=2,
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.permission_denied, 2)

    def test_prefetched_windows_measurements_avoid_second_process_reads(self):
        proc = FakeProcess()
        proc.cpu_error = AssertionError("CPU metrics should be prefetched")
        proc.memory_error = AssertionError("memory metrics should be prefetched")
        result = ProcessInventory().collect(
            {
                100: ProcessRecord(
                    proc=proc,
                    pid=100,
                    ppid=None,
                    name="python.exe",
                    created_at=100.0,
                    measurements=ProcessMeasurements(
                        cpu_total_sec=2.0,
                        memory_rss_bytes=32 * 1024 * 1024,
                    ),
                )
            },
            sampled_at=200,
            monotonic_at=1,
        )
        self.assertEqual(result.items[0].memory_mb, 32.0)
        self.assertEqual(result.items[0].category, PYTHON)

    def test_unsampled_process_reuses_values_with_original_sample_time(self):
        proc = FakeProcess(cpu=1.0, memory=64 * 1024 * 1024)
        inventory = ProcessInventory(cpu_count=1)
        first = inventory.collect(
            {100: record(proc=proc)}, sampled_at=100, monotonic_at=1
        )
        proc.cpu = 2.0
        proc.memory = 128 * 1024 * 1024
        second = inventory.collect(
            {100: record(proc=proc)}, sampled_at=101, monotonic_at=2
        )
        proc.cpu = 100.0
        proc.memory = 256 * 1024 * 1024
        skipped = ProcessRecord(
            proc=proc,
            pid=100,
            ppid=None,
            name="sample.exe",
            created_at=100.0,
            sample_metrics=False,
        )
        third = inventory.collect(
            {100: skipped}, sampled_at=103, monotonic_at=4
        )
        self.assertEqual(third.items[0].cpu_pct, second.items[0].cpu_pct)
        self.assertEqual(third.items[0].memory_mb, 128.0)
        self.assertEqual(third.items[0].cpu_sampled_at, 101)
        self.assertEqual(third.items[0].memory_sampled_at, 101)


class ProcessClassificationTests(unittest.TestCase):
    def test_known_categories_have_explanations(self):
        cases = (
            ("python.exe", PYTHON),
            ("node.exe", JAVASCRIPT),
            ("chrome.exe", BROWSERS),
        )
        for name, expected in cases:
            with self.subTest(name=name):
                result = classify_process(name)
                self.assertEqual(result.category, expected)
                self.assertTrue(result.reason.startswith("Matched"))

    def test_unknown_fallback_does_not_infer_from_arbitrary_name(self):
        result = classify_process("my-private-worker.exe")
        self.assertEqual(result.category, UNKNOWN)
        self.assertIn("No conservative", result.reason)


class ProcessModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.model = ProcessTableModel()
        self.proxy = ProcessFilterProxyModel()
        self.proxy.setSourceModel(self.model)

    def snapshot(self, sequence, items, complete=True):
        return ProcessSnapshot(
            items=tuple(items),
            sampled_at=1000 + sequence,
            sequence=sequence,
            complete=complete,
        )

    def test_pid_reuse_replaces_row_and_complete_snapshot_removes_terminated(self):
        self.model.apply_snapshot(self.snapshot(1, [sample(10, created=1.0)]))
        self.model.apply_snapshot(
            self.snapshot(2, [sample(10, created=2.0, name="new.exe")], complete=False)
        )
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.sample_at(0).identity.created_at, 2.0)

        self.model.apply_snapshot(self.snapshot(3, [], complete=True))
        self.assertEqual(self.model.rowCount(), 0)

    def test_partial_snapshot_retains_unseen_rows_and_rejects_stale_sequence(self):
        self.model.apply_snapshot(self.snapshot(2, [sample(10), sample(11)]))
        self.model.apply_snapshot(
            self.snapshot(3, [sample(10, cpu=5)], complete=False)
        )
        self.assertEqual(self.model.rowCount(), 2)
        self.assertFalse(self.model.apply_snapshot(self.snapshot(2, [])))

    def test_stale_resource_values_explain_their_sample_age(self):
        value = sample(10, cpu_sampled_at=100, memory_sampled_at=100)
        self.model.apply_snapshot(
            ProcessSnapshot((value,), sampled_at=105, sequence=1, complete=True)
        )
        cpu_tooltip = self.model.index(0, 3).data(Qt.ItemDataRole.ToolTipRole)
        memory_tooltip = self.model.index(0, 4).data(Qt.ItemDataRole.ToolTipRole)
        self.assertIn("5 seconds ago", cpu_tooltip)
        self.assertIn("5 seconds ago", memory_tooltip)

    def test_search_category_filter_and_numeric_sort(self):
        items = [
            sample(200, name="chrome.exe", category=BROWSERS),
            sample(10, name="python.exe", category=PYTHON),
        ]
        self.model.apply_snapshot(self.snapshot(1, items))
        self.proxy.set_search_text("CHROME")
        self.assertEqual(self.proxy.rowCount(), 1)
        self.assertEqual(self.proxy.index(0, 0).data(), "chrome.exe")

        self.model.apply_snapshot(
            self.snapshot(2, [sample(200, name="edge.exe", category=BROWSERS), items[1]])
        )
        self.assertEqual(self.proxy.rowCount(), 0)

        self.proxy.set_search_text("")
        self.proxy.set_category(PYTHON)
        self.assertEqual(self.proxy.rowCount(), 1)
        self.model.apply_snapshot(
            self.snapshot(3, [items[0], sample(10, name="node.exe", category=JAVASCRIPT)])
        )
        self.assertEqual(self.proxy.rowCount(), 0)
        self.proxy.set_category("All categories")
        self.proxy.sort(1, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.proxy.index(0, 1).data(), "10")
        self.assertEqual(self.proxy.index(1, 1).data(), "200")

    def test_active_filter_applies_to_processes_added_later(self):
        self.proxy.set_category(PYTHON)
        self.model.apply_snapshot(
            self.snapshot(1, [sample(10, name="chrome.exe", category=BROWSERS)])
        )
        self.assertEqual(self.proxy.rowCount(), 0)
        self.model.apply_snapshot(
            self.snapshot(2, [
                sample(10, name="chrome.exe", category=BROWSERS),
                sample(20, name="python.exe", category=PYTHON),
            ])
        )
        self.assertEqual(self.proxy.rowCount(), 1)
        self.assertEqual(self.proxy.index(0, 0).data(), "python.exe")

    def test_offscreen_process_window_accepts_snapshot_and_search(self):
        window = ProcessWindow()
        window.apply_snapshot(
            self.snapshot(1, [sample(10, name="python.exe", category=PYTHON)])
        )
        self.assertEqual(window.source_model.rowCount(), 1)
        self.assertIn("1 observed processes", window.status.text())
        window.search.setText("python")
        self.app.processEvents()
        self.assertEqual(window.proxy_model.rowCount(), 1)
        window.close()


class ProcessPrivacyTests(unittest.TestCase):
    def test_sensitive_history_metadata_is_opt_in(self):
        metadata = {
            "pid": 12,
            "process_name": "python.exe",
            "ppid": 3,
            "exe": r"C:\Users\person\python.exe",
            "cmdline": "python secret_task.py --token=redacted",
            "cwd": r"C:\Users\person\private",
            "script_label": "secret_task.py",
            "script_path": r"C:\Users\person\private\secret_task.py",
            "module_name": None,
            "project_path": r"C:\Users\person\private",
        }
        safe = history_metadata(metadata)
        self.assertEqual(safe["pid"], 12)
        self.assertEqual(safe["process_name"], "python.exe")
        self.assertEqual(safe["script_label"], "Python workload")
        for field in ("exe", "cmdline", "cwd", "script_path", "project_path", "ppid"):
            self.assertIsNone(safe[field])
        opted_in = history_metadata(metadata, include_private_details=True)
        self.assertEqual(opted_in["cmdline"], metadata["cmdline"])

        from storage import Storage

        with tempfile.TemporaryDirectory(prefix="procorbit-private-history-") as isolated:
            storage = Storage(Path(isolated) / "history.db")
            try:
                session_id = storage.create_session({
                    **safe,
                    "first_seen_at": 1.0,
                    "last_seen_at": 1.0,
                })
                row = storage.conn.execute(
                    """SELECT ppid, exe, cmdline, cwd, script_label,
                              script_path, module_name, project_path
                       FROM sessions WHERE id=?""",
                    (session_id,),
                ).fetchone()
                self.assertEqual(row[4], "Python workload")
                for index in (0, 1, 2, 3, 5, 6, 7):
                    self.assertIsNone(row[index])
            finally:
                storage.close()


class WorkspaceIsolationTests(unittest.TestCase):
    def test_proc_orbit_uses_separate_runtime_and_mutex_names(self):
        with tempfile.TemporaryDirectory(prefix="procorbit-isolation-") as isolated:
            with patch.dict(os.environ, {"LOCALAPPDATA": isolated}):
                import config
                from instance import SingleInstanceGuard

                self.assertEqual(config.APP_NAME, "ProcOrbit")
                self.assertFalse(config.DEFAULT_CONFIG["persist_python_metadata"])
                self.assertEqual(config.app_dir(), Path(isolated) / "ProcOrbit")
                self.assertFalse((Path(isolated) / "PythonWatch").exists())
                self.assertEqual(
                    SingleInstanceGuard.MUTEX_NAME,
                    "Local\\ProcOrbit.Instance",
                )
                self.assertEqual(
                    SingleInstanceGuard.EVENT_NAME,
                    "Local\\ProcOrbit.Show",
                )


if __name__ == "__main__":
    unittest.main()
