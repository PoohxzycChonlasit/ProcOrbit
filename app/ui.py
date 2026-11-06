from __future__ import annotations

import base64
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psutil
from PySide6.QtCore import QLocale, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from config import APP_NAME, CONFIG_PATH, DB_PATH, load_config, save_config
from monitor import MonitorWorker, script_info
from process_inventory import ProcessSnapshot
from process_window import ProcessWindow


COLORS = {
    "bg": "#0B0F0C",
    "surface": "#101612",
    "row": "#121A14",
    "hover": "#19231B",
    "border": "#263329",
    "text": "#A9C9AD",
    "secondary": "#79937D",
    "muted": "#586B5B",
    "accent": "#6FA576",
    "bright": "#88B98E",
    "WATCH": "#B89B58",
    "HIGH": "#C27F55",
    "CRITICAL": "#C56C70",
    "HEALTHY": "#6FA576",
}


def fmt_value(value, unit: str = "", digits: int = 1):
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}{unit}"
    except (TypeError, ValueError):
        return "—"


def fmt_memory(value):
    if value is None:
        return "—"
    value = float(value)
    return f"{value / 1024:.2f} GB" if value >= 1024 else f"{value:.0f} MB"


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def fmt_dt(ts):
    if not ts:
        return "Unavailable"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError, OverflowError):
        return "Unavailable"


def short_path(value: str | None, limit: int = 38):
    if not value:
        return "Project unavailable"
    value = str(value)
    if value in {"Unavailable", "Access denied"}:
        return value
    if len(value) <= limit:
        return value
    pieces = value.replace("/", "\\").rstrip("\\").split("\\")
    tail = "\\".join(pieces[-2:]) if len(pieces) > 1 else value[-limit:]
    return "…\\" + tail


def app_icon() -> QIcon:
    icon_path = Path(__file__).resolve().parent / "pythonwatch.ico"
    icon = QIcon(str(icon_path))
    if not icon.isNull():
        return icon
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(COLORS["bg"]))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(COLORS["accent"]), 2))
    painter.drawRoundedRect(5, 6, 22, 17, 3, 3)
    painter.drawLine(16, 23, 16, 27)
    painter.drawLine(11, 27, 21, 27)
    for (x1, y1), (x2, y2) in zip(
        [(9, 18), (13, 18), (16, 12), (19, 17)],
        [(13, 18), (16, 12), (19, 17), (23, 11)],
    ):
        painter.drawLine(x1, y1, x2, y2)
    painter.end()
    return QIcon(pixmap)


def startup_shortcut_path() -> Path:
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "ProcOrbit.lnk"


def current_launch_paths():
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    installed_root = program_files / APP_NAME
    installed_pythonw = installed_root / ".venv" / "Scripts" / "pythonw.exe"
    installed_main = installed_root / "app" / "main.py"
    if installed_pythonw.is_file() and installed_main.is_file():
        return installed_pythonw, installed_main

    main_py = Path(__file__).resolve().with_name("main.py")
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name(
        "pythonw.exe" if interpreter.name.lower() == "python.exe" else interpreter.name
    )
    if not pythonw.exists():
        pythonw = interpreter
    return pythonw, main_py


def set_startup_shortcut(enabled: bool) -> None:
    shortcut = startup_shortcut_path()
    if not enabled:
        if shortcut.exists():
            shortcut.unlink()
        return
    pythonw, main_py = current_launch_paths()
    icon_path = Path(__file__).resolve().with_name("pythonwatch.ico")
    args = f'-B "{main_py}"'
    escaped = lambda value: str(value).replace("'", "''")
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$link = $shell.CreateShortcut('{escaped(shortcut)}'); "
        f"$link.TargetPath = '{escaped(pythonw)}'; "
        f"$link.Arguments = '{escaped(args)}'; "
        f"$link.WorkingDirectory = '{escaped(main_py.parent.parent)}'; "
        "$link.Description = 'ProcOrbit process monitor'; "
        f"$link.IconLocation = '{escaped(icon_path)}'; "
        "$link.Save()"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-WindowStyle", "Hidden", "-EncodedCommand", encoded,
        ],
        check=True,
        timeout=12,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def detail_read(proc, method: str):
    try:
        value = getattr(proc, method)()
        if method == "cmdline":
            return subprocess.list2cmdline([str(x) for x in value]) if value else "Unavailable"
        return str(value) if value is not None else "Unavailable"
    except psutil.AccessDenied:
        return "Access denied"
    except (psutil.Error, OSError, ProcessLookupError):
        return "Unavailable"


def virtualenv_for_executable(executable):
    if not executable or executable in {"Unavailable", "Access denied"}:
        return None
    try:
        exe_path = Path(executable)
        if exe_path.parent.name.lower() == "scripts":
            candidate = exe_path.parent.parent
            if (candidate / "pyvenv.cfg").is_file():
                return str(candidate)
    except (OSError, ValueError):
        pass
    return None


def exact_detail_fields(pid, expected_created_at):
    fields = {"exe": "Unavailable", "cmdline": "Unavailable", "cwd": "Unavailable"}
    if expected_created_at is None:
        return {**{key: "Identity unavailable" for key in fields}, "_argv": None}
    try:
        proc = psutil.Process(int(pid))
        actual = proc.create_time()
        if abs(float(actual) - float(expected_created_at)) > 0.01:
            return {
                **{key: "Process ended or PID was reused" for key in fields},
                "_argv": None,
            }
    except psutil.AccessDenied:
        return {**{key: "Access denied" for key in fields}, "_argv": None}
    except (psutil.Error, OSError, ProcessLookupError, ValueError, TypeError):
        return {**fields, "_argv": None}
    try:
        argv = proc.cmdline()
        fields["cmdline"] = (
            subprocess.list2cmdline([str(x) for x in argv])
            if argv else "Unavailable"
        )
    except psutil.AccessDenied:
        argv = None
        fields["cmdline"] = "Access denied"
    except (psutil.Error, OSError, ProcessLookupError):
        argv = None
    fields["exe"] = detail_read(proc, "exe")
    fields["cwd"] = detail_read(proc, "cwd")
    return {**fields, "_argv": list(argv) if argv else None}


def enrich_details(row: dict):
    parent = dict(row.get("parent_process") or {})
    if parent.get("pid"):
        details = exact_detail_fields(parent["pid"], parent.get("created_at"))
        parent["exe"] = details["exe"]
        parent["cmdline"] = details["cmdline"]
    else:
        parent["exe"] = parent["cmdline"] = "Unavailable"

    tree = []
    for source in row.get("process_tree", []):
        node = dict(source)
        details = exact_detail_fields(node.get("pid"), node.get("created_at"))
        node.update({key: value for key, value in details.items() if key != "_argv"})
        if node.get("is_python"):
            cwd = node.get("cwd")
            parse_cwd = cwd if cwd not in {"Unavailable", "Access denied"} else None
            label, script_path, module_name, project_path = script_info(
                details.get("_argv"), node.get("name") or "python", parse_cwd
            )
            node["script_label"] = label
            node["script_path"] = script_path
            node["module_name"] = module_name
            node["project_path"] = project_path
            node["virtualenv_path"] = virtualenv_for_executable(node.get("exe"))
        tree.append(node)
    enriched = dict(row)
    enriched["parent_process"] = parent
    enriched["process_tree"] = tree
    return enriched


class ProcessRow(QFrame):
    clicked = Signal()

    def __init__(self, row: dict, parent=None):
        super().__init__(parent)
        self.row = row
        self._name_full = str(row.get("script_label") or row.get("process_name") or "Python")
        self._path_full = str(row.get("project_path") or "Project unavailable")
        self.setObjectName("processRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(53)
        self.setMaximumHeight(62)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(7, 5, 7, 5)
        outer.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(5)

        dot = QLabel("●")
        dot.setStyleSheet(f"color:{COLORS.get(row.get('health'), COLORS['accent'])};")
        dot.setFixedWidth(12)
        name = QLabel(self._name_full)
        name.setObjectName("processName")
        name.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        name.setMinimumWidth(0)
        name.setToolTip(self._name_full)
        cpu = QLabel(f"{fmt_value(row.get('tree_cpu_pct'), '%', 0)}")
        ram = QLabel(fmt_memory(row.get("tree_rss_mb")))
        elapsed = QLabel(fmt_duration(row.get("elapsed_s")))
        for label in (cpu, ram, elapsed):
            label.setObjectName("metric")
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(dot)
        top.addWidget(name, 1)
        top.addWidget(cpu)
        top.addWidget(ram)
        top.addWidget(elapsed)
        outer.addLayout(top)

        lower = QHBoxLayout()
        lower.setSpacing(4)
        path = QLabel(short_path(self._path_full))
        path.setObjectName("path")
        path.setMinimumWidth(0)
        path.setToolTip(self._path_full)
        self.name_label = name
        self.path_label = path
        lower.addWidget(path, 1)
        if row.get("child_count"):
            children = QLabel(f"{row['child_count']} child processes")
            children.setObjectName("path")
            lower.addWidget(children)
        if row.get("tree_cpu_unknown") or row.get("tree_ram_unknown"):
            partial = QLabel("partial metrics")
            partial.setObjectName("warning")
            lower.addWidget(partial)
        outer.addLayout(lower)

        for label in (dot, name, cpu, ram, elapsed, path):
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_elided_text)

    def _update_elided_text(self):
        if not self.name_label or not self.path_label:
            return
        name_width = max(0, self.name_label.width() - 4)
        path_width = max(0, self.path_label.width() - 4)
        self.name_label.setText(
            self.name_label.fontMetrics().elidedText(
                self._name_full, Qt.ElideMiddle, name_width
            )
        )
        self.path_label.setText(
            self.path_label.fontMetrics().elidedText(
                short_path(self._path_full), Qt.ElideMiddle, path_width
            )
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class HistoryRow(QFrame):
    clicked = Signal()

    def __init__(self, row: dict, parent=None):
        super().__init__(parent)
        self.row = row
        self.setObjectName("processRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(48)
        box = QVBoxLayout(self)
        box.setContentsMargins(6, 4, 6, 4)
        box.setSpacing(2)
        health = row.get("run_health") or {}
        score = health.get("score")
        if score is None:
            score_text = "Not enough data"
        else:
            score_text = f"{score}  {health.get('grade') or 'Unrated'}"
        confidence = health.get("confidence") or "LOW"
        name = str(row.get("script_label") or row.get("process_name") or "Python")
        self.title = QLabel(
            f"{score_text} · {name} · {fmt_duration(row.get('duration_s'))}"
        )
        self.title.setObjectName("processName")
        self.title.setWordWrap(True)
        self.title.setToolTip(name)
        metrics = health.get("metrics") or {}
        coverage = (
            " · INCOMPLETE SAMPLE COVERAGE"
            if metrics.get("sample_evidence_complete") is False else ""
        )
        cpu = metrics.get("tree_cpu_avg_pct", row.get("avg_cpu_pct"))
        ram = metrics.get("tree_ram_peak_mb", row.get("max_rss_mb"))
        end_reason = row.get("end_reason") or "End state unavailable"
        if metrics.get("monitor_interrupted") and end_reason == "process_ended":
            end_text = "Monitor restarted; root end later observed"
        elif metrics.get("monitor_interrupted"):
            end_text = "Monitor restarted; end uncertain"
        elif end_reason == "process_ended":
            end_text = "Root end observed"
        elif end_reason == "ended_while_monitor_off":
            end_text = "End not observed"
        else:
            end_text = "Observation ended; process end unknown"
        self.summary = QLabel(
            f"RAM peak {fmt_memory(ram)} · CPU avg {fmt_value(cpu, '%')} of host · "
            f"{confidence} confidence · {end_text}{coverage}"
        )
        self.summary.setObjectName("path")
        self.summary.setWordWrap(True)
        self.summary.setToolTip(short_path(row.get("project_path") or row.get("cwd"), 60))
        box.addWidget(self.title)
        box.addWidget(self.summary)
        for label in (self.title, self.summary):
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class SettingsDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ProcOrbit Settings")
        self.setWindowIcon(app_icon())
        self.config = config
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(7)

        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(7)
        self.startup = QCheckBox("Start with Windows")
        self.startup.setChecked(startup_shortcut_path().exists())
        self.hidden = QCheckBox("Launch hidden to tray")
        self.hidden.setChecked(bool(config.get("launch_hidden", True)))
        self.notifications = QCheckBox("Health notifications")
        self.notifications.setChecked(bool(config.get("notifications_enabled", True)))
        self.persist_python_metadata = QCheckBox(
            "Save detailed Python process metadata in history"
        )
        self.persist_python_metadata.setChecked(
            bool(config.get("persist_python_metadata", False))
        )
        self.visible_poll = self._double(config.get("poll_interval_sec", 2.0), 1.0, 10.0)
        self.hidden_poll = self._double(config.get("hidden_poll_interval_sec", 3.0), 2.0, 30.0)
        self.retention = QSpinBox()
        self.retention.setLocale(QLocale.c())
        self.retention.setRange(1, 90)
        self.retention.setValue(int(config.get("sample_retention_days", 7)))
        form.addRow(self.startup)
        form.addRow(self.hidden)
        form.addRow(self.notifications)
        form.addRow(self.persist_python_metadata)
        form.addRow("Visible polling (seconds)", self.visible_poll)
        form.addRow("Hidden polling (seconds)", self.hidden_poll)
        form.addRow("Sample retention (days)", self.retention)
        self.settings_scroll.setWidget(form_widget)
        root.addWidget(self.settings_scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        available_width = available.width() if available else 380
        available_height = available.height() if available else 500
        width = min(380, max(1, available_width))
        height = min(360, max(1, available_height - min(48, available_height // 8)))
        self.setFixedWidth(width)
        self.setMinimumHeight(min(220, height))
        self.setMaximumHeight(height)
        self.resize(width, height)

    @staticmethod
    def _double(value, minimum, maximum):
        box = QDoubleSpinBox()
        box.setLocale(QLocale.c())
        box.setRange(minimum, maximum)
        box.setSingleStep(0.5)
        box.setDecimals(1)
        box.setValue(float(value))
        return box

    def values(self):
        updated = dict(self.config)
        updated["launch_hidden"] = self.hidden.isChecked()
        updated["notifications_enabled"] = self.notifications.isChecked()
        updated["persist_python_metadata"] = self.persist_python_metadata.isChecked()
        updated["poll_interval_sec"] = self.visible_poll.value()
        updated["hidden_poll_interval_sec"] = self.hidden_poll.value()
        updated["sample_retention_days"] = self.retention.value()
        return updated, self.startup.isChecked()


class MonitorWindow(QMainWindow):
    HISTORY_PAGE_SIZE = 50

    def __init__(self, worker: MonitorWorker, icon: QIcon, config: dict, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.icon = icon
        self.config = config
        self.active_rows: list[dict] = []
        self.history_rows: list[dict] = []
        self.latest_system: dict = {}
        self.paused = False
        self.monitor_error: str | None = None
        self.detail_return_page = None
        self.history_generation = 0
        self.history_cursor: tuple[float, int] | None = None
        self.history_page_cursor: tuple[float, int] | None = None
        self.history_previous_cursors: list[tuple[float, int] | None] = []
        self.history_has_more = False
        self.history_loading = False
        self.history_request_action = "refresh"
        self.history_request_cursor: tuple[float, int] | None = None
        self.history_retry: tuple[str, tuple[float, int] | None] | None = None
        self._geometry_initialized = False
        self._restoring_geometry = False
        self._process_window: ProcessWindow | None = None
        self.latest_process_snapshot: ProcessSnapshot | None = None

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icon)
        self.setMinimumSize(340, 230)
        self.setWindowFlags(
            Qt.Window | Qt.WindowSystemMenuHint | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint
        )
        self.resize(430, 340)

        root = QWidget()
        root.setObjectName("root")
        root.setStyleSheet(f"""
            QWidget#root {{ background:{COLORS['bg']}; border:1px solid {COLORS['border']}; }}
            QLabel {{ color:{COLORS['text']}; font-family:'Segoe UI'; }}
            QLabel#appTitle {{ color:{COLORS['bright']}; font-size:13px; font-weight:700; }}
            QLabel#status {{ color:{COLORS['accent']}; font-size:10px; }}
            QLabel#summary {{ color:{COLORS['secondary']}; font-size:11px; }}
            QLabel#processName {{ color:{COLORS['text']}; font-size:11px; font-weight:600; }}
            QLabel#metric {{ color:{COLORS['bright']}; font-size:10px; }}
            QLabel#path {{ color:{COLORS['secondary']}; font-size:9px; }}
            QLabel#warning {{ color:{COLORS['WATCH']}; font-size:9px; }}
            QPushButton {{ background:transparent; color:{COLORS['secondary']}; border:1px solid transparent; padding:3px 7px; min-height:24px; max-height:28px; font-family:'Segoe UI'; font-size:10px; }}
            QPushButton:hover {{ background:{COLORS['hover']}; color:{COLORS['bright']}; border-color:{COLORS['border']}; }}
            QFrame#processRow {{ background:transparent; border-bottom:1px solid {COLORS['border']}; }}
            QFrame#processRow:hover {{ background:{COLORS['hover']}; }}
            QScrollArea {{ border:0; background:transparent; }}
            QScrollBar:vertical {{ width:8px; background:{COLORS['surface']}; }}
            QScrollBar::handle:vertical {{ background:{COLORS['border']}; min-height:22px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        """)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(9, 7, 9, 8)
        outer.setSpacing(5)

        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("ProcOrbit")
        title.setObjectName("appTitle")
        self.status = QLabel("● LIVE")
        self.status.setObjectName("status")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.status)
        outer.addLayout(header)

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setStyleSheet(f"color:{COLORS['border']};")
        outer.addWidget(rule)

        self.stack = QStackedWidget()
        self.main_page = self._build_main_page()
        self.detail_page = self._build_detail_page()
        self.history_page = self._build_history_page()
        self.stack.addWidget(self.main_page)
        self.stack.addWidget(self.detail_page)
        self.stack.addWidget(self.history_page)
        outer.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        footer.setSpacing(4)
        footer.addStretch()
        self.history_button = QPushButton("History")
        self.history_button.clicked.connect(self.show_history)
        self.processes_button = QPushButton("All processes")
        self.processes_button.clicked.connect(self.show_all_processes)
        self.settings_button = QPushButton("Settings")
        footer.addWidget(self.processes_button)
        footer.addWidget(self.history_button)
        footer.addWidget(self.settings_button)
        outer.addLayout(footer)

        self.worker.snapshot.connect(self.on_snapshot)
        self.worker.process_snapshot_ready.connect(self.on_process_snapshot)
        self.worker.history_page_ready.connect(self.on_history_page)
        self.worker.monitor_error.connect(self.on_error)
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(350)
        self._geometry_save_timer.timeout.connect(self.save_geometry)
        self._restore_geometry()

    def _build_main_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.summary = QLabel("Starting monitor…")
        self.summary.setObjectName("summary")
        layout.addWidget(self.summary)

        self.list_scroll = QScrollArea()
        self.list_scroll.setWidgetResizable(True)
        self.list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.rows_widget = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(1)
        self.rows_layout.setAlignment(Qt.AlignTop)
        self.list_scroll.setWidget(self.rows_widget)
        layout.addWidget(self.list_scroll, 1)
        return page

    def _build_detail_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.back_detail = QPushButton("‹  Back to workloads")
        self.back_detail.clicked.connect(self.back_from_detail)
        layout.addWidget(self.back_detail)
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.detail_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.detail_content = QWidget()
        self.detail_layout = QVBoxLayout(self.detail_content)
        self.detail_layout.setContentsMargins(2, 2, 2, 4)
        self.detail_layout.setSpacing(5)
        self.detail_scroll.setWidget(self.detail_content)
        layout.addWidget(self.detail_scroll, 1)
        return page

    def _build_history_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.back_history = QPushButton("‹  Back to workloads")
        self.back_history.clicked.connect(self.show_main)
        layout.addWidget(self.back_history)
        self.history_status = QLabel("History has not been loaded.")
        self.history_status.setObjectName("summary")
        layout.addWidget(self.history_status)
        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.history_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.history_content = QWidget()
        self.history_layout = QVBoxLayout(self.history_content)
        self.history_layout.setContentsMargins(1, 1, 1, 1)
        self.history_layout.setSpacing(3)
        self.history_layout.setAlignment(Qt.AlignTop)
        self.history_scroll.setWidget(self.history_content)
        layout.addWidget(self.history_scroll, 1)
        self.history_more_button = QPushButton("Load more history")
        self.history_more_button.setVisible(False)
        self.history_more_button.clicked.connect(self.load_more_history)
        self.history_newer_button = QPushButton("Newer runs")
        self.history_newer_button.setVisible(False)
        self.history_newer_button.clicked.connect(self.load_newer_history)
        history_navigation = QHBoxLayout()
        history_navigation.addWidget(self.history_newer_button)
        history_navigation.addStretch(1)
        history_navigation.addWidget(self.history_more_button)
        layout.addLayout(history_navigation)
        return page

    def attach_worker(self, worker: MonitorWorker):
        self.worker = worker
        worker.snapshot.connect(self.on_snapshot)
        worker.process_snapshot_ready.connect(self.on_process_snapshot)
        worker.history_page_ready.connect(self.on_history_page)
        worker.monitor_error.connect(self.on_error)

    def show_all_processes(self):
        if self._process_window is None:
            self._process_window = ProcessWindow(self)
        if self.latest_process_snapshot is not None:
            self._process_window.apply_snapshot(self.latest_process_snapshot)
        self._process_window.show()
        self._process_window.raise_()
        self._process_window.activateWindow()

    def on_process_snapshot(self, snapshot: ProcessSnapshot):
        self.latest_process_snapshot = snapshot
        if self._process_window is not None:
            self._process_window.apply_snapshot(snapshot)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def on_snapshot(self, active, system):
        self.active_rows = list(active)
        self.latest_system = dict(system)
        self.monitor_error = None
        self.status.setText("● PAUSED" if system.get("paused") else "● LIVE")
        self.status.setStyleSheet(
            f"color:{COLORS['WATCH'] if system.get('paused') else COLORS['accent']};"
        )
        count = int(system.get("python_count") or 0)
        cpu = fmt_value(system.get("python_cpu_pct"), "%", 0)
        ram = fmt_memory(system.get("python_rss_mb"))
        self.summary.setText(f"{count} running  •  Workload CPU {cpu}  •  RAM {ram}")
        self._update_tooltip(count, cpu, ram)
        if self.stack.currentWidget() == self.main_page:
            self._fill_rows()

    def _update_tooltip(self, count, cpu, ram):
        if count:
            text = f"ProcOrbit\n{count} Python workloads running\nCPU {cpu} • RAM {ram}"
        else:
            text = "ProcOrbit\nNo Python workloads running"
        if hasattr(self, "tray"):
            self.tray.setToolTip(text)

    def _fill_rows(self):
        scroll_value = self.list_scroll.verticalScrollBar().value()
        self._clear_layout(self.rows_layout)
        if not self.active_rows:
            empty = QLabel(
                "No Python workloads running\nMonitoring is active · "
                + datetime.now().strftime("Last scan %H:%M:%S")
            )
            empty.setObjectName("summary")
            empty.setWordWrap(True)
            empty.setMinimumHeight(50)
            self.rows_layout.addWidget(empty)
        else:
            for row in self.active_rows:
                item = ProcessRow(row)
                item.clicked.connect(lambda checked=False, r=row: self.show_detail(r))
                self.rows_layout.addWidget(item)
        self.rows_layout.addStretch(1)
        QTimer.singleShot(
            0,
            lambda value=scroll_value: self.list_scroll.verticalScrollBar().setValue(value),
        )

    def _request_history_page(self, cursor, *, action="refresh"):
        self.history_loading = True
        self.history_status.setText("Loading history…")
        self.history_more_button.setEnabled(False)
        self.history_newer_button.setEnabled(False)
        self.history_more_button.setText("Loading…")
        accepted = self.worker.request_history_page(
            self.history_generation, cursor, self.HISTORY_PAGE_SIZE
        )
        if accepted:
            self.history_request_action = action
            self.history_request_cursor = cursor
            return True

        self.history_loading = False
        self.history_retry = (action, cursor)
        self.history_status.setText("History request queue is busy. Try again.")
        self.history_more_button.setVisible(True)
        self.history_more_button.setEnabled(True)
        self.history_more_button.setText("Retry history")
        self.history_newer_button.setEnabled(bool(self.history_previous_cursors))
        return False

    def on_history_page(self, page):
        if page.get("generation") != self.history_generation:
            return
        self.history_loading = False
        if page.get("error"):
            self.history_retry = (
                self.history_request_action,
                self.history_request_cursor,
            )
            self.history_status.setText("Could not load history. Try again.")
            self.history_more_button.setVisible(True)
            self.history_more_button.setEnabled(True)
            self.history_more_button.setText("Retry history")
            self.history_newer_button.setEnabled(bool(self.history_previous_cursors))
            return

        self.history_retry = None
        action = self.history_request_action
        if action == "older":
            self.history_previous_cursors.append(self.history_page_cursor)
        elif action == "newer" and self.history_previous_cursors:
            self.history_previous_cursors.pop()
        elif action == "refresh":
            self.history_previous_cursors.clear()
        self.history_page_cursor = page.get("cursor")
        rows = list(page.get("rows") or [])
        self.history_rows = rows
        self._clear_layout(self.history_layout)
        for row in rows:
            item = HistoryRow(row)
            item.clicked.connect(lambda checked=False, r=row: self.show_history_detail(r))
            self.history_layout.addWidget(item)
        self.history_cursor = page.get("next_cursor")
        self.history_has_more = bool(page.get("has_more"))
        self.history_newer_button.setVisible(bool(self.history_previous_cursors))
        self.history_newer_button.setEnabled(bool(self.history_previous_cursors))
        self.history_more_button.setVisible(self.history_has_more)
        self.history_more_button.setEnabled(self.history_has_more)
        self.history_more_button.setText("Older runs")
        if not self.history_rows:
            self.history_status.setText("No completed workload history yet.")
        else:
            status = f"Page {len(self.history_previous_cursors) + 1} · {len(self.history_rows)} runs shown"
            if self.history_has_more:
                status += " · older runs are available"
            self.history_status.setText(status)
        QTimer.singleShot(
            0, lambda: self.history_scroll.verticalScrollBar().setValue(0)
        )

    def load_more_history(self):
        if self.history_loading:
            return
        if self.history_retry is not None:
            action, cursor = self.history_retry
            self._request_history_page(cursor, action=action)
            return
        if self.history_cursor is None:
            if not self.history_rows:
                self._request_history_page(None, action="refresh")
            return
        self._request_history_page(self.history_cursor, action="older")

    def load_newer_history(self):
        if self.history_loading:
            return
        if self.history_retry is not None:
            action, cursor = self.history_retry
            self._request_history_page(cursor, action=action)
            return
        if not self.history_previous_cursors:
            return
        self._request_history_page(
            self.history_previous_cursors[-1], action="newer"
        )

    def _add_detail_field(self, key, value):
        row = QFrame()
        row.setObjectName("processRow")
        layout = QVBoxLayout(row)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(1)
        label = QLabel(str(key).upper())
        label.setObjectName("path")
        content = QLabel(str(value) if value not in (None, "") else "Unavailable")
        content.setWordWrap(True)
        content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        content.setStyleSheet(f"color:{COLORS['text']}; font-size:10px;")
        layout.addWidget(label)
        layout.addWidget(content)
        self.detail_layout.addWidget(row)

    def show_detail(self, original_row):
        row = enrich_details(original_row)
        self.detail_return_page = self.main_page
        self.back_detail.setText("‹  Back to workloads")
        self._clear_layout(self.detail_layout)
        nested = int(row.get("child_count") or 0)
        workload_type = (
            f"Root workload · {nested} descendants"
            if row.get("root_workload_pid") == row.get("pid")
            else "Nested Python process"
        )
        fields = [
            ("Workload", row.get("script_label")),
            ("Project", row.get("project_path")),
            ("Script path", row.get("script_path")),
            ("Python module (-m)", row.get("module_name") or "Not invoked with -m"),
            ("Identity", f"PID {row.get('pid')} · PPID {row.get('ppid')} · {workload_type}"),
            ("Parent", self._parent_text(row.get("parent_process"))),
            ("Python executable", row.get("exe")),
            ("Virtual environment", row.get("virtualenv_path") or "Not detected"),
            ("Full command", row.get("cmdline")),
            ("Working directory", row.get("cwd")),
            ("Started", fmt_dt(row.get("started_at"))),
            ("Elapsed", fmt_duration(row.get("elapsed_s"))),
            ("Own CPU / RAM", f"{fmt_value(row.get('own_cpu_pct'), '%')} · {fmt_memory(row.get('own_rss_mb'))} ({fmt_value(row.get('own_mem_pct'), '%', 2)})"),
            ("Descendant CPU / RAM", f"{fmt_value(row.get('children_cpu_pct'), '%')} · {fmt_memory(row.get('children_rss_mb'))}"),
            ("Whole tree CPU / RAM", f"{fmt_value(row.get('tree_cpu_pct'), '%')} · {fmt_memory(row.get('tree_rss_mb'))}"),
            ("Threads", row.get("own_threads")),
            ("Own disk I/O", f"Read {fmt_value(row.get('own_read_mb_s'), ' MB/s', 2)} · Write {fmt_value(row.get('own_write_mb_s'), ' MB/s', 2)}"),
            ("Health", f"{row.get('health')} · {'; '.join(row.get('health_reasons') or []) or 'No current warning'}"),
            ("Resource availability", f"CPU unavailable for {row.get('tree_cpu_unknown', 0)} process(es); RAM unavailable for {row.get('tree_ram_unknown', 0)} process(es)"),
            ("Progress", self._progress(row)),
            ("Root confidence", row.get("root_confidence")),
        ]
        for key, value in fields:
            self._add_detail_field(key, value)

        tree_header = QLabel("PROCESS TREE")
        tree_header.setObjectName("processName")
        self.detail_layout.addWidget(tree_header)
        for node in row.get("process_tree", []):
            prefix = "  " * int(node.get("depth") or 0)
            connector = "└─ " if node.get("depth") else ""
            role = " · nested Python" if node.get("is_python") and node.get("depth") else ""
            elapsed = None
            if node.get("created_at") is not None and node.get("snapshot_at") is not None:
                elapsed = max(0.0, node["snapshot_at"] - node["created_at"])
            node_lines = [
                f"{prefix}{connector}{node.get('name')}  PID {node.get('pid')}"
                f" · PPID {node.get('ppid') if node.get('ppid') is not None else '—'}{role}",
                f"Started {fmt_dt(node.get('created_at'))} · Elapsed {fmt_duration(elapsed)}",
                f"Own {fmt_value(node.get('cpu_pct'), '%')} · {fmt_memory(node.get('rss_mb'))}"
                f" · {fmt_value(node.get('mem_pct'), '%', 2)} RAM",
                f"Subtree {fmt_value(node.get('subtree_cpu_pct'), '%')} · "
                f"{fmt_memory(node.get('subtree_rss_mb'))}",
                f"Threads {fmt_value(node.get('threads'), '', 0)} · "
                f"Read {fmt_value(node.get('read_mb_s'), ' MB/s', 2)} · "
                f"Write {fmt_value(node.get('write_mb_s'), ' MB/s', 2)}",
            ]
            if node.get("is_python"):
                node_lines.extend([
                    f"Workload {node.get('script_label') or 'Unavailable'} · "
                    f"Module {node.get('module_name') or '—'}",
                    f"Script {node.get('script_path') or 'Unavailable'}",
                    f"Project {node.get('project_path') or 'Unavailable'}",
                    f"Virtual environment {node.get('virtualenv_path') or 'Not detected'}",
                ])
            node_lines.extend([
                f"EXE {node.get('exe') or 'Unavailable'}",
                f"CMD {node.get('cmdline') or 'Unavailable'}",
                f"CWD {node.get('cwd') or 'Unavailable'}",
            ])
            node_text = "\n".join(node_lines)
            self._add_detail_field("  " * int(node.get("depth") or 0) + "Process", node_text)
        self.detail_layout.addStretch(1)
        self.stack.setCurrentWidget(self.detail_page)
        QTimer.singleShot(0, lambda: self.detail_scroll.verticalScrollBar().setValue(0))

    def show_history_detail(self, row: dict):
        self.detail_return_page = self.history_page
        self.back_detail.setText("‹  Back to history")
        self._clear_layout(self.detail_layout)
        health = row.get("run_health") or {}
        metrics = health.get("metrics") or {}
        score = health.get("score")
        health_text = (
            "Not enough data"
            if score is None else
            f"{score} / 100 · {health.get('grade') or 'Unrated'}"
        )
        components = health.get("components") or []
        known_weight = health.get("known_weight")
        confidence = health.get("confidence") or "LOW"
        exe = row.get("exe") or "Unavailable"
        virtualenv = virtualenv_for_executable(exe) or "Not detected"
        coverage = (
            "Complete"
            if metrics.get("sample_evidence_complete") is True else
            "Incomplete"
            if metrics.get("sample_evidence_complete") is False else
            "Unavailable"
        )
        sample_window = (
            f"{coverage}; first stored sample {fmt_duration(metrics.get('sample_start_gap_s'))} "
            f"after first observation; last stored sample {fmt_duration(metrics.get('sample_end_gap_s'))} "
            f"before root end; largest gap {fmt_duration(metrics.get('max_sample_gap_s'))}. "
            f"{metrics.get('sample_evidence_detail') or ''}"
        )
        root_end = (
            "Observed"
            if row.get("end_reason") == "process_ended" and row.get("ended_at") is not None
            else "Unknown"
        )
        fields = [
            ("Run health", health_text),
            ("Grade and confidence", f"{health.get('grade') or 'Unavailable'} · {confidence}"),
            (
                "Evidence coverage",
                f"{known_weight if known_weight is not None else 0} / 100 component-weight points available; "
                "the score is normalized across available components.",
            ),
            ("Sample coverage", sample_window),
            ("Started", fmt_dt(row.get("created_at"))),
            ("First observed", fmt_dt(row.get("first_seen_at"))),
            ("End time", fmt_dt(row.get("ended_at")) if row.get("ended_at") is not None else "Unknown; monitoring ended without observing root termination"),
            ("Duration", fmt_duration(row.get("duration_s"))),
            ("Workload", row.get("script_label") or row.get("process_name") or "Unavailable"),
            ("Executable", exe),
            ("Virtual environment", virtualenv),
            ("Command", row.get("cmdline") or "Unavailable"),
            ("Working directory / project", row.get("project_path") or row.get("cwd") or "Unavailable"),
            ("Script path", row.get("script_path") or "Unavailable"),
            ("Python module (-m)", row.get("module_name") or "Not invoked with -m / unavailable"),
            ("Identity", f"PID {row.get('pid')} · PPID {row.get('ppid') or 'Unavailable'}"),
            (
                "CPU",
                f"Tree average {fmt_value(metrics.get('tree_cpu_avg_pct', row.get('avg_cpu_pct')), '%')} · "
                f"peak {fmt_value(metrics.get('tree_cpu_peak_pct', row.get('max_cpu_pct')), '%')} of host capacity; "
                f"host average {fmt_value(metrics.get('host_cpu_avg_pct'), '%')} · "
                f"peak {fmt_value(metrics.get('host_cpu_peak_pct'), '%')}.",
            ),
            (
                "Memory",
                f"Tree average {fmt_memory(metrics.get('tree_ram_avg_mb', row.get('avg_rss_mb')))} · "
                f"peak {fmt_memory(metrics.get('tree_ram_peak_mb', row.get('max_rss_mb')))} "
                f"({fmt_value(metrics.get('tree_ram_peak_host_pct', row.get('peak_mem_pct')), '%', 2)} of host RAM); "
                f"host average {fmt_value(metrics.get('host_ram_avg_pct'), '%')} · "
                f"peak {fmt_value(metrics.get('host_ram_peak_pct'), '%')}.",
            ),
            (
                "Process tree",
                f"Root PID {row.get('pid')} · up to {metrics.get('peak_descendants_observed', 'unknown')} descendants observed.",
            ),
            (
                "End state",
                f"{root_end} root end · {row.get('tree_end_state') or 'unknown'} tree state. "
                f"{row.get('tree_end_detail') or 'Exit code is not collected.'}",
            ),
            (
                "Monitoring continuity",
                "ProcOrbit restarted while this workload was active; score confidence is capped at LOW."
                if metrics.get("monitor_interrupted") else
                "No ProcOrbit restart was recorded during this observation."
                if row.get("end_reason") == "process_ended" else
                "Monitoring ended before the root process end was confirmed.",
            ),
            (
                "Important limitation",
                "This score describes observed machine/resource conditions and process observation. "
                "It is not a speed benchmark or proof that the workload completed successfully.",
            ),
        ]
        for key, value in fields:
            self._add_detail_field(key, value)

        heading = QLabel("WHY THIS SCORE?")
        heading.setObjectName("processName")
        self.detail_layout.addWidget(heading)
        if components:
            for component in components:
                if component.get("available"):
                    score_line = f"{component.get('points')} / {component.get('weight')}"
                else:
                    score_line = f"UNKNOWN / {component.get('weight')}"
                self._add_detail_field(
                    component.get("name") or "Component",
                    f"{score_line}\n{component.get('explanation') or 'No explanation available.'}",
                )
        else:
            self._add_detail_field(
                "Assessment",
                "This legacy history entry has no stored host-pressure evidence or versioned score. "
                "No score has been backfilled from incomplete data.",
            )
        for explanation in health.get("explanations") or []:
            self._add_detail_field("Evidence note", explanation)
        self.detail_layout.addStretch(1)
        self.stack.setCurrentWidget(self.detail_page)
        QTimer.singleShot(0, lambda: self.detail_scroll.verticalScrollBar().setValue(0))

    def back_from_detail(self):
        if self.detail_return_page == self.history_page:
            self.stack.setCurrentWidget(self.history_page)
            return
        self.show_main()

    @staticmethod
    def _parent_text(parent):
        if not parent:
            return "Unavailable"
        return (
            f"{parent.get('name') or 'Unavailable'} · PID {parent.get('pid') or '—'}\n"
            f"EXE {parent.get('exe') or 'Unavailable'}\n"
            f"CMD {parent.get('cmdline') or 'Unavailable'}"
        )

    @staticmethod
    def _progress(row):
        if row.get("progress_pct") is None:
            return "OS cannot infer semantic progress; no heartbeat reported"
        parts = [f"{row['progress_pct']:.0f}%"]
        if row.get("progress_phase"):
            parts.append(str(row["progress_phase"]))
        if row.get("progress_message"):
            parts.append(str(row["progress_message"]))
        return " · ".join(parts)

    def show_history(self):
        self.stack.setCurrentWidget(self.history_page)
        self.history_generation += 1
        self.history_rows.clear()
        self.history_cursor = None
        self.history_page_cursor = None
        self.history_previous_cursors.clear()
        self.history_has_more = False
        self.history_loading = False
        self.history_retry = None
        self._clear_layout(self.history_layout)
        self.history_more_button.setVisible(False)
        self._request_history_page(None, action="refresh")

    def show_main(self):
        self.stack.setCurrentWidget(self.main_page)
        self._fill_rows()

    def on_error(self, message):
        self.monitor_error = str(message)
        self.status.setText("● ERROR")
        self.status.setStyleSheet(f"color:{COLORS['CRITICAL']};")
        self.status.setToolTip(str(message))

    def toggle_pause(self):
        self.paused = not self.paused
        self.worker.set_paused(self.paused)
        self.status.setText("● PAUSED" if self.paused else "● LIVE")
        self.status.setStyleSheet(
            f"color:{COLORS['WATCH'] if self.paused else COLORS['accent']};"
        )

    def _restore_geometry(self):
        screens = QGuiApplication.screens()
        screen = QGuiApplication.primaryScreen() or (screens[0] if screens else None)
        saved_rect = None
        saved = self.config.get("window_geometry")
        if isinstance(saved, dict):
            try:
                rect = QRect(
                    int(saved["x"]), int(saved["y"]),
                    int(saved["width"]), int(saved["height"]),
                )
                if rect.width() > 0 and rect.height() > 0:
                    best_screen = None
                    best_area = 0
                    for candidate in screens:
                        intersection = rect.intersected(candidate.availableGeometry())
                        area = max(0, intersection.width()) * max(0, intersection.height())
                        if area > best_area:
                            best_area = area
                            best_screen = candidate
                    if best_screen is not None:
                        screen = best_screen
                        saved_rect = rect
            except (KeyError, TypeError, ValueError):
                saved_rect = None

        if screen is None:
            self._geometry_initialized = True
            return
        area = screen.availableGeometry()
        min_width = min(340, max(1, area.width()))
        min_height = min(230, max(1, area.height()))
        self.setMinimumSize(min_width, min_height)
        if saved_rect is not None:
            width = max(min_width, min(saved_rect.width(), area.width()))
            height = max(min_height, min(saved_rect.height(), area.height()))
            x = max(area.left(), min(saved_rect.x(), area.right() - width + 1))
            y = max(area.top(), min(saved_rect.y(), area.bottom() - height + 1))
        else:
            width = min(max(min_width, 430), area.width())
            height = min(max(min_height, 340), area.height())
            x = max(area.left(), area.right() - width - 16)
            y = max(area.top(), area.bottom() - height - 16)
        self._restoring_geometry = True
        self.setGeometry(QRect(x, y, width, height))
        self._restoring_geometry = False
        self._geometry_initialized = True

    def _schedule_geometry_save(self):
        if (
            self._geometry_initialized
            and not self._restoring_geometry
            and not self.isMaximized()
            and not self.isMinimized()
        ):
            self._geometry_save_timer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_geometry_save()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_geometry_save()

    def save_geometry(self):
        if not self._geometry_initialized:
            return
        rect = self.normalGeometry() if (self.isMaximized() or self.isMinimized()) else self.geometry()
        if rect.isEmpty():
            return
        self.config["window_geometry"] = {
            "x": rect.x(),
            "y": rect.y(),
            "width": rect.width(),
            "height": rect.height(),
        }
        try:
            save_config(self.config)
        except (OSError, TypeError, ValueError) as exc:
            self.monitor_error = f"Could not save window geometry: {exc}"

    def show_near_tray(self, preserve_page=False):
        if not preserve_page:
            self.show_main()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self.worker.set_visible(True)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):
        self._geometry_save_timer.stop()
        self.save_geometry()
        self.worker.set_visible(False)
        super().hideEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()


class PythonWatchApp:
    def __init__(self, instance_guard=None, *, show_initially=False):
        self.guard = instance_guard
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setApplicationName(APP_NAME)
        self.icon = app_icon()
        self.app.setWindowIcon(self.icon)
        self.config = load_config()
        self.worker = MonitorWorker()
        self.window = MonitorWindow(self.worker, self.icon, self.config)
        self.window.settings_button.clicked.connect(self.show_settings)

        self.tray = QSystemTrayIcon(self.icon)
        self.tray.setToolTip("ProcOrbit\nStarting monitor…")
        self.window.tray = self.tray

        menu = QMenu()
        self._add_action(menu, "Show ProcOrbit", self.show_window)
        self._add_action(menu, "Hide ProcOrbit", self.window.hide)
        menu.addSeparator()
        self._add_action(menu, "History", self.show_history)
        self._add_action(menu, "Settings", self.show_settings)
        menu.addSeparator()
        self._add_action(menu, "Restart monitor", self.restart_monitor)
        self._add_action(menu, "Exit ProcOrbit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.worker.alert.connect(self.on_alert)
        self.tray.show()

        self.show_request_timer = QTimer(self.app)
        self.show_request_timer.setInterval(1000)
        self.show_request_timer.timeout.connect(self.check_show_request)
        self.show_request_timer.start()

        self.worker.start()
        if show_initially or not bool(self.config.get("launch_hidden", True)):
            QTimer.singleShot(0, self.show_window)

    @staticmethod
    def _add_action(menu, title, callback):
        action = QAction(title)
        action.triggered.connect(callback)
        menu.addAction(action)

    def check_show_request(self):
        if self.guard and self.guard.show_requested():
            self.show_window()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_window()

    def show_window(self):
        self.window.show_near_tray()

    def show_history(self):
        self.window.show_history()
        self.window.show_near_tray(preserve_page=True)

    def show_settings(self):
        dialog = SettingsDialog(self.config, self.window)
        if dialog.exec() == QDialog.Accepted:
            try:
                config, startup_enabled = dialog.values()
                save_config(config)
                set_startup_shortcut(startup_enabled)
                self.config = config
                self.window.config = config
                self.worker.cfg = config
            except Exception as exc:
                QMessageBox.warning(
                    self.window, "ProcOrbit Settings",
                    f"Could not save settings:\n{exc}",
                )

    def on_alert(self, severity, message):
        if not self.config.get("notifications_enabled", True):
            return
        icon = (
            QSystemTrayIcon.Critical
            if severity == "CRITICAL"
            else QSystemTrayIcon.Warning
        )
        self.tray.showMessage(
            f"ProcOrbit — {severity}", message, icon, 6000
        )

    def restart_monitor(self):
        was_visible = self.window.isVisible()
        old = self.worker
        old.stop()
        if not old.wait(6000):
            QMessageBox.warning(
                self.window, "ProcOrbit",
                "The monitor has not stopped yet; no second worker was started.",
            )
            return
        worker = MonitorWorker()
        self.worker = worker
        self.window.attach_worker(worker)
        worker.alert.connect(self.on_alert)
        worker.start()
        if was_visible:
            self.window.worker.set_visible(True)

    def quit(self):
        self.show_request_timer.stop()
        self.window.save_geometry()
        self.worker.stop()
        if not self.worker.wait(6000):
            self.show_request_timer.start()
            QMessageBox.warning(
                self.window,
                "ProcOrbit",
                "The monitor has not stopped yet. ProcOrbit is still running.",
            )
            return
        self.tray.hide()
        self.app.quit()

    def run(self):
        return self.app.exec()
