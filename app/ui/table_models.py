from collections.abc import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

SORT_ROLE = Qt.ItemDataRole.UserRole


class ObjectTableModel(QAbstractTableModel):
    """Generic read-only table model over a list of arbitrary objects.

    `columns` is a list of (header, getter) or (header, getter, sort_key)
    tuples. `getter(obj)` produces the display string; `sort_key(obj)`
    (optional) produces the raw, directly-comparable value used for sorting
    and grouping — e.g. a float for a "£150.00" column, or a date for a
    "01 Aug 2025" column — since string-sorting formatted display text gives
    the wrong order for numbers and dates. Defaults to the getter's own
    return value when no sort_key is given, which is correct for plain text
    columns.
    """

    def __init__(self, columns: list[tuple], rows=None, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows = rows or []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        col = self.columns[index.column()]
        obj = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return col[1](obj)
        if role == SORT_ROLE:
            sort_key = col[2] if len(col) > 2 else col[1]
            return sort_key(obj)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section][0]
        return str(section + 1)

    def set_rows(self, rows: list) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def object_at(self, row: int):
        return self.rows[row]
