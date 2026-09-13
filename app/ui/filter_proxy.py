from PySide6.QtCore import QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor

from app.ui import theme
from app.ui.table_models import SORT_ROLE

_BAND_COLORS = (QColor(theme.SURFACE), QColor(theme.SURFACE_ALT))


class GroupFilterProxyModel(QSortFilterProxyModel):
    """Adds three things on top of the base ObjectTableModel:

    - type-to-filter across every column (not just one filterKeyColumn)
    - a "group by" column: rows are primarily sorted by that column's value,
      with the normal column-click sort acting as the secondary/tiebreak key
    - alternating background bands per group (instead of per row) while
      grouping is active, so same-group rows are visually clustered
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter_text = ""
        self._group_column = -1
        self.setDynamicSortFilter(True)

    def set_filter_text(self, text: str) -> None:
        self._filter_text = text.strip().lower()
        self.invalidateFilter()

    def set_group_column(self, column: int) -> None:
        self._group_column = column
        self.invalidate()
        self.sort(self.sortColumn() if self.sortColumn() >= 0 else 0, self.sortOrder())

    def filterAcceptsRow(self, source_row, source_parent) -> bool:
        if not self._filter_text:
            return True
        model = self.sourceModel()
        for col in range(model.columnCount()):
            value = model.data(model.index(source_row, col, source_parent), Qt.ItemDataRole.DisplayRole)
            if value and self._filter_text in str(value).lower():
                return True
        return False

    def lessThan(self, left, right) -> bool:
        model = self.sourceModel()
        if self._group_column >= 0:
            left_group = model.data(model.index(left.row(), self._group_column), SORT_ROLE)
            right_group = model.data(model.index(right.row(), self._group_column), SORT_ROLE)
            if left_group != right_group:
                return self._safe_less(left_group, right_group)
        left_val = model.data(model.index(left.row(), left.column()), SORT_ROLE)
        right_val = model.data(model.index(right.row(), right.column()), SORT_ROLE)
        return self._safe_less(left_val, right_val)

    @staticmethod
    def _safe_less(a, b) -> bool:
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.BackgroundRole and self._group_column >= 0:
            band = self._band_for_row(index.row())
            return _BAND_COLORS[band % 2]
        return super().data(index, role)

    def _band_for_row(self, proxy_row: int) -> int:
        band = 0
        previous = object()  # sentinel that can't equal any real group value
        for row in range(proxy_row + 1):
            value = self.data(self.index(row, self._group_column), SORT_ROLE)
            if value != previous:
                band += 1
                previous = value
        return band


class PageFilterProxyModel(QSortFilterProxyModel):
    """Windows a (possibly filtered/sorted) source model down to one page's
    worth of rows — large tables (thousands of transactions across every
    statement) were rendering and being scrolled through all at once.

    Stack this on top of the model you actually want to filter/sort/group
    (e.g. GroupFilterProxyModel) rather than replacing it: `source_row` in
    filterAcceptsRow refers to the source model's own exposed row order,
    which for a QSortFilterProxyModel source already reflects its sort —
    so windowing here naturally paginates the final sorted/filtered result.
    Header-click sorting is forwarded through to the source model so it
    keeps working transparently with a page proxy on top.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = 0
        self._page_size = 100
        self.setDynamicSortFilter(True)

    def set_page_size(self, size: int) -> None:
        self._page_size = max(1, size)
        self._page = 0
        self.invalidateFilter()

    def set_page(self, page: int) -> None:
        self._page = max(0, min(page, self.page_count() - 1))
        self.invalidateFilter()

    def reset_page(self) -> None:
        self._page = 0
        self.invalidateFilter()

    @property
    def page(self) -> int:
        return self._page

    @property
    def page_size(self) -> int:
        return self._page_size

    def total_row_count(self) -> int:
        source = self.sourceModel()
        return source.rowCount() if source is not None else 0

    def page_count(self) -> int:
        return max(1, -(-self.total_row_count() // self._page_size))  # ceil division

    def filterAcceptsRow(self, source_row, source_parent) -> bool:
        start = self._page * self._page_size
        return start <= source_row < start + self._page_size

    def sort(self, column, order=Qt.SortOrder.AscendingOrder) -> None:
        source = self.sourceModel()
        if source is not None:
            source.sort(column, order)
        self.invalidateFilter()
