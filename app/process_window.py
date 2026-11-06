from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTableView,
    QVBoxLayout,
    QHeaderView,
)

from process_classifier import CATEGORIES
from process_inventory import ProcessSnapshot
from process_model import ProcessFilterProxyModel, ProcessTableModel


class ProcessWindow(QDialog):
    """A read-only, in-memory view of all visible local processes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ProcOrbit — All processes")
        self.setMinimumSize(900, 500)
        self.resize(1120, 680)

        self.source_model = ProcessTableModel(self)
        self.proxy_model = ProcessFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.source_model)

        root = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search process, PID, category, parent, or path")
        self.category = QComboBox()
        self.category.addItems(["All categories", *CATEGORIES])
        controls.addWidget(self.search, 1)
        controls.addWidget(self.category)
        root.addLayout(controls)

        self.table = QTableView()
        self.table.setModel(self.proxy_model)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

        self.status = QLabel("Waiting for first process snapshot…")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.status)

        self.search.textChanged.connect(self.proxy_model.set_search_text)
        self.category.currentTextChanged.connect(self.proxy_model.set_category)
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)

    def apply_snapshot(self, snapshot: ProcessSnapshot) -> None:
        if not self.source_model.apply_snapshot(snapshot):
            return
        shown = self.source_model.rowCount()
        denied = snapshot.permission_denied
        if snapshot.complete:
            completeness = "complete enumeration"
        else:
            completeness = "partial scan; unseen rows retained"
        suffix = f" · {denied} restricted field reads" if denied else ""
        self.status.setText(
            f"{shown} observed processes · {completeness}{suffix} · "
            "snapshots stay in memory"
        )
