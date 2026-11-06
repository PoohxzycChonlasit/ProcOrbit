from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    QTimer,
    Qt,
)

from process_classifier import UNKNOWN
from process_inventory import ProcessSample, ProcessSnapshot


HEADERS = (
    "Process",
    "PID",
    "Category",
    "CPU (% host)",
    "Memory (MB)",
    "Uptime",
    "Parent",
    "Executable",
)

_SORT_ROLE = Qt.ItemDataRole.UserRole + 1
_SAMPLE_ROLE = Qt.ItemDataRole.UserRole + 2


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _display(sample: ProcessSample, column: int) -> str:
    if column == 0:
        return sample.name or "Unknown process"
    if column == 1:
        return str(sample.identity.pid)
    if column == 2:
        return sample.category or UNKNOWN
    if column == 3:
        return "—" if sample.cpu_pct is None else f"{sample.cpu_pct:.1f}%"
    if column == 4:
        return "—" if sample.memory_mb is None else f"{sample.memory_mb:.1f}"
    if column == 5:
        return _duration(sample.uptime_sec)
    if column == 6:
        if sample.parent_pid is None:
            return "—"
        if sample.parent_name:
            return f"{sample.parent_name} ({sample.parent_pid})"
        return f"PID {sample.parent_pid}"
    if column == 7:
        if sample.executable:
            return sample.executable
        if sample.executable_status:
            return sample.executable_status
        return "Unavailable"
    return ""


def _sort_value(sample: ProcessSample, column: int):
    numeric = {
        1: sample.identity.pid,
        3: sample.cpu_pct,
        4: sample.memory_mb,
        5: sample.uptime_sec,
        6: sample.parent_pid,
    }
    if column in numeric:
        value = numeric[column]
        # Unknown metrics sort below known values in ascending order.
        return (value is not None, value if value is not None else -1)
    return _display(sample, column).casefold()


class ProcessTableModel(QAbstractTableModel):
    """Incremental source model keyed by PID plus process creation time."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[ProcessSample] = []
        self._row_by_identity = {}
        self._sequence = 0
        self._snapshot_at: float | None = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(HEADERS):
                return HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        sample = self._items[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return _display(sample, column)
        if role == _SORT_ROLE:
            return _sort_value(sample, column)
        if role == _SAMPLE_ROLE:
            return sample
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 1 and not sample.identity.verified:
                return "Creation time unavailable; PID reuse cannot be distinguished."
            if column == 2:
                return sample.category_reason
            if column == 3 and sample.cpu_status:
                return sample.cpu_status
            if column == 4 and sample.memory_status:
                return sample.memory_status
            if column == 3 and sample.cpu_sampled_at is not None and self._snapshot_at is not None:
                age = max(0.0, self._snapshot_at - sample.cpu_sampled_at)
                if age >= 1:
                    return f"Last CPU sample {age:.0f} seconds ago."
            if column == 4 and sample.memory_sampled_at is not None and self._snapshot_at is not None:
                age = max(0.0, self._snapshot_at - sample.memory_sampled_at)
                if age >= 1:
                    return f"Last memory sample {age:.0f} seconds ago."
            if column == 7 and sample.executable_status:
                return sample.executable_status
        if role == Qt.ItemDataRole.TextAlignmentRole and column in {1, 3, 4, 5}:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def sample_at(self, row: int) -> ProcessSample | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def apply_snapshot(self, snapshot: ProcessSnapshot) -> bool:
        """Apply a new snapshot without resetting unchanged rows."""
        if snapshot.sequence <= self._sequence:
            return False
        self._sequence = snapshot.sequence
        self._snapshot_at = snapshot.sampled_at

        incoming = {item.identity: item for item in snapshot.items}
        incoming_pids = {identity.pid for identity in incoming}
        remove_rows = {
            row for row, old in enumerate(self._items)
            if (
                old.identity.pid in incoming_pids
                and old.identity not in incoming
            ) or (
                snapshot.complete and old.identity not in incoming
            )
        }
        # Remove contiguous ranges from bottom to top so row indexes remain valid.
        ranges = []
        ordered = sorted(remove_rows)
        if ordered:
            start = previous = ordered[0]
            for row in ordered[1:]:
                if row == previous + 1:
                    previous = row
                    continue
                ranges.append((start, previous))
                start = previous = row
            ranges.append((start, previous))
        for first, last in reversed(ranges):
            self.beginRemoveRows(QModelIndex(), first, last)
            del self._items[first:last + 1]
            self.endRemoveRows()

        self._row_by_identity = {
            item.identity: row for row, item in enumerate(self._items)
        }
        changed_by_column: dict[int, list[int]] = {}
        additions = []
        for identity, sample in incoming.items():
            row = self._row_by_identity.get(identity)
            if row is None:
                additions.append(sample)
            else:
                old = self._items[row]
                changed_columns = []
                if old.name != sample.name:
                    changed_columns.append(0)
                if (
                    old.category != sample.category
                    or old.category_reason != sample.category_reason
                ):
                    changed_columns.append(2)
                if (
                    old.cpu_pct != sample.cpu_pct
                    or old.cpu_status != sample.cpu_status
                    or old.cpu_sampled_at != sample.cpu_sampled_at
                ):
                    changed_columns.append(3)
                if (
                    old.memory_mb != sample.memory_mb
                    or old.memory_status != sample.memory_status
                    or old.memory_sampled_at != sample.memory_sampled_at
                ):
                    changed_columns.append(4)
                if old.uptime_sec != sample.uptime_sec:
                    changed_columns.append(5)
                if (
                    old.parent_pid != sample.parent_pid
                    or old.parent_name != sample.parent_name
                ):
                    changed_columns.append(6)
                if (
                    old.executable != sample.executable
                    or old.executable_status != sample.executable_status
                ):
                    changed_columns.append(7)
                self._items[row] = sample
                for column in changed_columns:
                    changed_by_column.setdefault(column, []).append(row)

        for column, changed_rows in changed_by_column.items():
            changed_rows.sort()
            first = previous = changed_rows[0]
            ranges = []
            for row in changed_rows[1:]:
                if row == previous + 1:
                    previous = row
                    continue
                ranges.append((first, previous))
                first = previous = row
            ranges.append((first, previous))
            for first, last in ranges:
                self.dataChanged.emit(
                    self.index(first, column),
                    self.index(last, column),
                    [
                        Qt.ItemDataRole.DisplayRole,
                        Qt.ItemDataRole.ToolTipRole,
                        _SORT_ROLE,
                        _SAMPLE_ROLE,
                    ],
                )

        if additions:
            first = len(self._items)
            last = first + len(additions) - 1
            self.beginInsertRows(QModelIndex(), first, last)
            self._items.extend(additions)
            self.endInsertRows()
        self._row_by_identity = {
            item.identity: row for row, item in enumerate(self._items)
        }
        return True


class ProcessFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._search = ""
        self._category = "All categories"
        self.setDynamicSortFilter(False)
        self.setSortRole(_SORT_ROLE)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._source_signal_model = None
        self._sort_pending = False

    def setSourceModel(self, source_model):
        previous = self._source_signal_model
        if previous is not None:
            try:
                previous.dataChanged.disconnect(self._source_data_changed)
            except (RuntimeError, TypeError):
                pass
        super().setSourceModel(source_model)
        self._source_signal_model = source_model
        if source_model is not None:
            source_model.dataChanged.connect(self._source_data_changed)
            source_model.rowsInserted.connect(self._source_rows_changed)
            source_model.rowsRemoved.connect(self._source_rows_changed)

    def _source_rows_changed(self, *_args):
        if self._search or self._category != "All categories":
            self.invalidate()
        self._queue_sort()

    def _queue_sort(self):
        if self.sortColumn() < 0 or self._sort_pending:
            return
        self._sort_pending = True
        QTimer.singleShot(0, self._sort_current)

    def _sort_current(self):
        self._sort_pending = False
        if self.sortColumn() >= 0:
            self.sort(self.sortColumn(), self.sortOrder())

    def _source_data_changed(self, top_left, bottom_right, _roles):
        text_columns = {0, 2, 6, 7}
        if top_left.column() <= self.sortColumn() <= bottom_right.column():
            self._queue_sort()
        if (
            (self._search or self._category != "All categories")
            and any(top_left.column() <= column <= bottom_right.column() for column in text_columns)
        ):
            self.invalidate()

    def set_search_text(self, value: str) -> None:
        self._search = (value or "").strip().casefold()
        self.invalidate()

    def set_category(self, value: str) -> None:
        self._category = value or "All categories"
        self.invalidate()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        sample = model.sample_at(source_row) if model is not None else None
        if sample is None:
            return False
        if self._category != "All categories" and sample.category != self._category:
            return False
        if not self._search:
            return True
        searchable = (
            sample.name or "",
            str(sample.identity.pid),
            sample.category or UNKNOWN,
            sample.parent_name or "",
            str(sample.parent_pid or ""),
            sample.executable or "",
            sample.category_reason,
        )
        needle = self._search
        return any(needle in value.casefold() for value in searchable)

    def lessThan(self, left, right):
        left_value = self.sourceModel().data(left, _SORT_ROLE)
        right_value = self.sourceModel().data(right, _SORT_ROLE)
        return left_value < right_value
