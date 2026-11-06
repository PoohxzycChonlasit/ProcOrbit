from __future__ import annotations

import os
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(APP))

import psutil  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer, Qt, Slot  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from process_classifier import BROWSERS, PYTHON  # noqa: E402
from process_window import ProcessWindow  # noqa: E402


def _wait_until(predicate, timeout_sec: float) -> bool:
    """Wait by running Qt's event loop so queued worker signals are delivered."""
    if predicate():
        return True

    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(10)
    deadline = time.monotonic() + timeout_sec

    def check():
        if predicate() or time.monotonic() >= deadline:
            loop.quit()

    poll.timeout.connect(check)
    poll.start()
    QTimer.singleShot(max(1, int(timeout_sec * 1000)), loop.quit)
    loop.exec()
    poll.stop()
    return predicate()


def _run_event_loop_for(duration_sec: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(max(1, int(duration_sec * 1000)), loop.quit)
    loop.exec()


class _RecordingProcessWindow(ProcessWindow):
    def __init__(self):
        super().__init__()
        self.snapshots = []
        self.model_apply_ms = []
        self.sample_to_model_ms = []
        self.delivered_on_gui_thread = True

    @Slot(object)
    def apply_snapshot(self, snapshot):
        started = time.perf_counter()
        self.delivered_on_gui_thread &= (
            QApplication.instance().thread() == self.thread()
        )
        self.snapshots.append(snapshot)
        super().apply_snapshot(snapshot)
        self.model_apply_ms.append((time.perf_counter() - started) * 1000)
        self.sample_to_model_ms.append(
            max(0.0, time.time() - snapshot.sampled_at) * 1000
        )


class RuntimeIntegrationTests(unittest.TestCase):
    def test_worker_signal_updates_live_process_window(self):
        app = QApplication.instance() or QApplication([])
        previous_quit_policy = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)

        with tempfile.TemporaryDirectory(prefix="procorbit-m004b-") as runtime_root:
            runtime_root = Path(runtime_root)
            isolated_app_dir = runtime_root / "ProcOrbit"
            isolated_app_dir.mkdir(parents=True)

            db_path = isolated_app_dir / "data" / "integration.db"
            heartbeat_dir = isolated_app_dir / "heartbeats"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(runtime_root)}):
                import config
                import monitor

                with (
                    patch.object(config, "CONFIG_PATH", isolated_app_dir / "config.json"),
                    patch.object(monitor, "DB_PATH", db_path),
                    patch.object(monitor, "HEARTBEAT_DIR", heartbeat_dir),
                ):
                    worker = monitor.MonitorWorker()
                    worker.cfg.update(
                        poll_interval_sec=2.0,
                        hidden_poll_interval_sec=3.0,
                        db_sample_interval_sec=3600.0,
                        notifications_enabled=False,
                        persist_python_metadata=False,
                    )
                    worker.set_visible(True)

                    window = _RecordingProcessWindow()
                    worker.process_snapshot_ready.connect(window.apply_snapshot)
                    window.show()

                    child = None
                    worker_started_at = time.perf_counter()
                    process = psutil.Process(os.getpid())
                    try:
                        worker.start()
                        initial_ready = _wait_until(
                            lambda: bool(window.snapshots)
                            and window.source_model.rowCount() > 0,
                            timeout_sec=35.0,
                        )
                        self.assertTrue(initial_ready, "initial process snapshot did not reach the model")
                        initial_ready_ms = (time.perf_counter() - worker_started_at) * 1000
                        first_sample_to_model_ms = window.sample_to_model_ms[0]
                        initial_process_count = window.source_model.rowCount()
                        self.assertGreater(initial_process_count, 0)

                        child = subprocess.Popen(
                            [sys.executable, "-c", "import time; time.sleep(60)"],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        child_pid = child.pid
                        child_seen = _wait_until(
                            lambda: any(
                                item.identity.pid == child_pid
                                for snapshot in window.snapshots
                                for item in snapshot.items
                            ),
                            timeout_sec=15.0,
                        )
                        self.assertTrue(child_seen, "controlled child did not appear in inventory snapshots")

                        child_samples = [
                            item
                            for snapshot in window.snapshots
                            for item in snapshot.items
                            if item.identity.pid == child_pid
                        ]
                        expected_identity = child_samples[0].identity
                        self.assertTrue(expected_identity.verified)
                        self.assertEqual(child_samples[0].category, PYTHON)
                        first_child_sequence = next(
                            snapshot.sequence
                            for snapshot in window.snapshots
                            if any(item.identity == expected_identity for item in snapshot.items)
                        )
                        later_identity_seen = _wait_until(
                            lambda: any(
                                snapshot.sequence > first_child_sequence
                                and any(
                                    item.identity == expected_identity
                                    for item in snapshot.items
                                )
                                for snapshot in window.snapshots
                            ),
                            timeout_sec=8.0,
                        )
                        self.assertTrue(later_identity_seen, "process identity changed between live snapshots")

                        search_started = time.perf_counter()
                        window.search.setText(str(child_pid))

                        def child_is_search_result():
                            visible_pids = [
                                window.proxy_model.index(row, 1).data()
                                for row in range(window.proxy_model.rowCount())
                            ]
                            return (
                                str(child_pid) in visible_pids
                                and len(visible_pids) < window.source_model.rowCount()
                            )

                        search_ready = _wait_until(
                            child_is_search_result,
                            timeout_sec=1.0,
                        )
                        search_latency_ms = (time.perf_counter() - search_started) * 1000
                        child_in_source = any(
                            window.source_model.sample_at(row).identity.pid == child_pid
                            for row in range(window.source_model.rowCount())
                        )
                        self.assertTrue(
                            search_ready,
                            "PID search did not isolate the controlled child "
                            f"(proxy_rows={window.proxy_model.rowCount()}, "
                            f"source_rows={window.source_model.rowCount()}, "
                            f"child_in_source={child_in_source}, "
                            f"search={window.search.text()!r}, child_alive={child.poll() is None})",
                        )

                        category_index = window.category.findText(PYTHON)
                        self.assertGreaterEqual(category_index, 0)
                        window.category.setCurrentIndex(category_index)
                        python_pids = {
                            window.proxy_model.index(row, 1).data()
                            for row in range(window.proxy_model.rowCount())
                        }
                        self.assertIn(str(child_pid), python_pids)
                        browser_index = window.category.findText(BROWSERS)
                        self.assertGreaterEqual(browser_index, 0)
                        window.category.setCurrentIndex(browser_index)
                        browser_pids = {
                            window.proxy_model.index(row, 1).data()
                            for row in range(window.proxy_model.rowCount())
                        }
                        self.assertNotIn(str(child_pid), browser_pids)

                        window.category.setCurrentText("All categories")
                        window.search.clear()
                        window.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
                        _run_event_loop_for(0.03)
                        sorted_pids = [
                            int(window.proxy_model.index(row, 1).data())
                            for row in range(window.proxy_model.rowCount())
                        ]
                        self.assertEqual(sorted_pids, sorted(sorted_pids))

                        last_before_termination = window.snapshots[-1].sequence
                        child.terminate()
                        child.wait(timeout=5)
                        inventory_absent = _wait_until(
                            lambda: any(
                                snapshot.sequence > last_before_termination
                                and all(item.identity.pid != child_pid for item in snapshot.items)
                                for snapshot in window.snapshots
                            ),
                            timeout_sec=15.0,
                        )
                        self.assertTrue(inventory_absent, "terminated child remained in a later inventory snapshot")

                        latest = window.snapshots[-1]
                        if latest.complete:
                            self.assertFalse(
                                any(
                                    window.source_model.sample_at(row).identity.pid == child_pid
                                    for row in range(window.source_model.rowCount())
                                ),
                                "complete snapshot did not remove the terminated process row",
                            )
                        else:
                            # Incomplete scans intentionally retain unseen rows
                            # to avoid false removals after permission failures.
                            self.assertTrue(
                                any(
                                    window.source_model.sample_at(row).identity.pid == child_pid
                                    for row in range(window.source_model.rowCount())
                                ),
                                "partial snapshot should retain unseen rows",
                            )

                        cpu_before = process.cpu_times()
                        cpu_started_at = time.perf_counter()
                        self.assertTrue(
                            _wait_until(lambda: time.perf_counter() - cpu_started_at >= 4.0, 6.0),
                            "Qt event loop did not complete the bounded CPU sample window",
                        )
                        cpu_after = process.cpu_times()
                        cpu_elapsed = time.perf_counter() - cpu_started_at
                        cpu_pct_one_core = (
                            (cpu_after.user + cpu_after.system - cpu_before.user - cpu_before.system)
                            / max(cpu_elapsed, 1e-9)
                            * 100.0
                        )
                        app_rss_mib = process.memory_info().rss / (1024 * 1024)

                        shutdown_started = time.perf_counter()
                        worker.stop()
                        worker_stopped = worker.wait(6000)
                        shutdown_ms = (time.perf_counter() - shutdown_started) * 1000
                        self.assertTrue(worker_stopped, "monitor worker did not stop gracefully within 6 seconds")
                        self.assertFalse(worker.isRunning())
                        self.assertTrue(window.delivered_on_gui_thread)
                        self.assertTrue(db_path.exists(), "isolated worker did not initialize its runtime database")

                        apply_median = statistics.median(window.model_apply_ms)
                        sample_to_model_median = statistics.median(window.sample_to_model_ms)
                        print(
                            "RUNTIME_INTEGRATION_METRICS "
                            f"event_loop=QEventLoop.exec "
                            f"initial_ready_ms={initial_ready_ms:.1f} "
                            f"initial_processes={initial_process_count} "
                            f"first_sample_start_to_model_ms={first_sample_to_model_ms:.1f} "
                            f"apply_median_ms={apply_median:.3f} "
                            f"sample_start_to_model_median_ms={sample_to_model_median:.1f} "
                            f"search_ms={search_latency_ms:.1f} "
                            f"cpu_pct_one_core={cpu_pct_one_core:.2f} "
                            f"test_host_rss_mib={app_rss_mib:.2f} "
                            f"shutdown_ms={shutdown_ms:.1f} "
                            f"terminated_snapshot_complete={latest.complete}"
                        )
                    finally:
                        if child is not None and child.poll() is None:
                            child.terminate()
                            child.wait(timeout=5)
                        if worker.isRunning():
                            worker.stop()
                            self.assertTrue(worker.wait(6000), "worker cleanup timed out")
                        window.close()
                        app.setQuitOnLastWindowClosed(previous_quit_policy)


if __name__ == "__main__":
    unittest.main()
