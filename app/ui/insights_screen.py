import datetime as dt
from types import SimpleNamespace

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QDate, QDateTime, QPointF, Qt, QTime
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.analytics import ExpenseSeries, GRANULARITIES, expense_vs_forecast
from app.models import Account, Category
from app.ui import theme
from app.ui.widgets import AccountMultiSelect
from app.vendor_groups import list_vendor_groups

GROUP_BY_OPTIONS = ["Category", "Vendor Group"]

# Cycled per selected category/vendor group — same palette used for the
# "split by account" cashflow chart, kept distinct from color so line style
# (solid/dot/dash) is what tells Actual/Moving Average/Forecast apart.
SERIES_COLORS = [
    theme.ACCENT,
    theme.SUCCESS,
    theme.WARNING,
    "#F2B705",
    "#B45BEF",
    "#05C7F2",
    "#F2905B",
    theme.TEXT_MUTED,
]


def _to_msecs(date: dt.date) -> float:
    return float(QDateTime(QDate(date.year, date.month, date.day), QTime(0, 0)).toMSecsSinceEpoch())


class InsightsScreen(QWidget):
    """Actual expense spend (with a rolling moving average) vs the spend
    implied by matching budget items — a forecast-vs-live trend view,
    grouped by either Category or Vendor Group, for spotting drift between
    what's budgeted and what's actually happening. Any number of
    categories/vendor groups can be overlaid on the same plot, and the
    Moving Average / Forecast lines can each be toggled off to reduce
    clutter.
    """

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self._value_names: dict[int, str] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Insights</h2>"))
        description = QLabel(
            "Actual expense spend and its moving average, overlaid against the spend implied by "
            "matching budget items — grouped by Category or Vendor Group. Select multiple values to "
            "compare them on the same plot."
        )
        description.setWordWrap(True)
        description.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(description)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Group by:"))
        self.group_by_combo = QComboBox()
        for g in GROUP_BY_OPTIONS:
            self.group_by_combo.addItem(g)
        self.group_by_combo.currentIndexChanged.connect(self._on_group_by_changed)
        controls.addWidget(self.group_by_combo)

        controls.addWidget(QLabel("Values:"))
        self.value_select = AccountMultiSelect(noun="category", noun_plural="categories")
        self.value_select.selectionChanged.connect(self.refresh)
        controls.addWidget(self.value_select)
        hint = QLabel("(none selected = combined total)")
        hint.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        controls.addWidget(hint)

        controls.addWidget(QLabel("Accounts:"))
        self.account_select = AccountMultiSelect()
        self.account_select.selectionChanged.connect(self.refresh)
        controls.addWidget(self.account_select)
        controls.addStretch()
        layout.addLayout(controls)

        controls2 = QHBoxLayout()
        controls2.addWidget(QLabel("Granularity:"))
        self.granularity_combo = QComboBox()
        for g in GRANULARITIES:
            self.granularity_combo.addItem(g)
        self.granularity_combo.setCurrentText("Monthly")
        self.granularity_combo.currentIndexChanged.connect(self.refresh)
        controls2.addWidget(self.granularity_combo)

        controls2.addWidget(QLabel("Moving average window:"))
        self.ma_spin = QSpinBox()
        self.ma_spin.setRange(1, 24)
        self.ma_spin.setValue(3)
        self.ma_spin.valueChanged.connect(self.refresh)
        controls2.addWidget(self.ma_spin)

        self.show_ma_check = QCheckBox("Show Moving Average")
        self.show_ma_check.setChecked(True)
        self.show_ma_check.toggled.connect(self.refresh)
        controls2.addWidget(self.show_ma_check)

        self.show_forecast_check = QCheckBox("Show Forecast")
        self.show_forecast_check.setChecked(True)
        self.show_forecast_check.toggled.connect(self.refresh)
        controls2.addWidget(self.show_forecast_check)

        controls2.addWidget(QLabel("From:"))
        self.from_edit = QDateEdit(QDate.currentDate().addYears(-1))
        self.from_edit.setCalendarPopup(True)
        self.from_edit.dateChanged.connect(self.refresh)
        controls2.addWidget(self.from_edit)

        controls2.addWidget(QLabel("To:"))
        self.to_edit = QDateEdit(QDate.currentDate())
        self.to_edit.setCalendarPopup(True)
        self.to_edit.dateChanged.connect(self.refresh)
        controls2.addWidget(self.to_edit)

        controls2.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        controls2.addWidget(refresh_btn)
        layout.addLayout(controls2)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.summary_label)

        layout.addWidget(self._build_chart())

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Group", "Period", "Actual", "Moving Avg", "Forecast"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        self.reload()

    # -- chart -----------------------------------------------------------

    def _build_chart(self) -> QChartView:
        self.chart = QChart()
        self.chart.legend().setVisible(True)
        self.chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        self.chart.legend().setLabelColor(QColor(theme.TEXT_MUTED))
        self.chart.setTitle("Actual vs Forecast")
        self.chart.setTitleBrush(QColor(theme.TEXT))
        self.chart.setBackgroundBrush(QColor(theme.SURFACE))
        self.chart.setBackgroundPen(QColor(theme.BORDER))
        self.chart.setPlotAreaBackgroundVisible(False)

        self._series_list: list[QLineSeries] = []

        self.x_axis = QDateTimeAxis()
        self.x_axis.setFormat("MMM yyyy")
        self.x_axis.setLabelsColor(QColor(theme.TEXT_MUTED))
        self.x_axis.setGridLineColor(QColor(theme.BORDER))
        self.x_axis.setLinePenColor(QColor(theme.BORDER))
        self.chart.addAxis(self.x_axis, Qt.AlignmentFlag.AlignBottom)

        self.y_axis = QValueAxis()
        self.y_axis.setLabelFormat("£%.0f")
        self.y_axis.setLabelsColor(QColor(theme.TEXT_MUTED))
        self.y_axis.setGridLineColor(QColor(theme.BORDER))
        self.y_axis.setLinePenColor(QColor(theme.BORDER))
        self.chart.addAxis(self.y_axis, Qt.AlignmentFlag.AlignLeft)

        chart_view = QChartView(self.chart)
        chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        chart_view.setMinimumHeight(260)
        chart_view.setMaximumHeight(320)
        chart_view.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        return chart_view

    def _set_series(self, specs: list[tuple[str, list[QPointF], str, Qt.PenStyle, float]]) -> None:
        """Replace every line on the chart. Each spec is (name, points,
        color_hex, pen_style, width) — called fresh on every refresh since
        the number of lines varies with how many values/toggles are active."""
        for series in self._series_list:
            self.chart.removeSeries(series)
        self._series_list = []

        for name, points, color, style, width in specs:
            series = QLineSeries()
            series.setName(name)
            pen = QPen(QColor(color))
            pen.setWidthF(width)
            pen.setStyle(style)
            series.setPen(pen)
            series.replace(points)
            self.chart.addSeries(series)
            series.attachAxis(self.x_axis)
            series.attachAxis(self.y_axis)
            self._series_list.append(series)

    # -- data --------------------------------------------------------------

    def reload(self):
        """Repopulate the Accounts filter and the Group-by Values picker
        (categories or vendor groups) — call on tab activation so newly
        added accounts/categories/vendor groups show up without restarting
        the app."""
        accounts = self.session.query(Account).order_by(Account.name).all()
        self.account_select.set_accounts(accounts)
        self._reload_value_select()
        self.refresh()

    def _on_group_by_changed(self):
        self._reload_value_select()
        self.refresh()

    def _reload_value_select(self):
        is_category = self.group_by_combo.currentText() == "Category"
        self.value_select.noun = "category" if is_category else "vendor group"
        self.value_select.noun_plural = "categories" if is_category else "vendor groups"
        self.value_select.all_selected_label = "All Categories" if is_category else "All Vendor Groups"
        if is_category:
            items = [
                SimpleNamespace(id=c.id, name=c.name)
                for c in self.session.query(Category).order_by(Category.name).all()
            ]
        else:
            items = [SimpleNamespace(id=g.id, name=g.name) for g in list_vendor_groups(self.session)]
        self._value_names = {item.id: item.name for item in items}
        self.value_select.set_accounts(items, default_all_checked=False)

    def _money_item(self, value: float) -> QTableWidgetItem:
        item = QTableWidgetItem(f"£{value:,.2f}")
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _selected_values(self) -> list[tuple[int | None, str]]:
        """(id, label) pairs to plot/table — one per checked value, or a
        single (None, "All ...") entry standing for the combined total when
        nothing's checked."""
        checked = self.value_select.checked_ids()
        if checked:
            return [(vid, self._value_names.get(vid, str(vid))) for vid in checked]
        is_category = self.group_by_combo.currentText() == "Category"
        return [(None, "All Categories" if is_category else "All Vendor Groups")]

    def refresh(self):
        sd, ed = self.from_edit.date(), self.to_edit.date()
        start = dt.date(sd.year(), sd.month(), sd.day())
        end = dt.date(ed.year(), ed.month(), ed.day())
        if end < start:
            self.summary_label.setText("End date is before start date.")
            self.table.setRowCount(0)
            self._set_series([])
            return

        account_ids = self.account_select.checked_ids() or None
        group_by = self.group_by_combo.currentText()
        granularity = self.granularity_combo.currentText()
        ma_window = self.ma_spin.value()
        show_ma = self.show_ma_check.isChecked()
        show_forecast = self.show_forecast_check.isChecked()

        series_by_value: list[tuple[str, ExpenseSeries]] = []
        for value_id, label in self._selected_values():
            s = expense_vs_forecast(
                self.session,
                granularity,
                start,
                end,
                ma_window=ma_window,
                category_id=value_id if group_by == "Category" else None,
                vendor_group_id=value_id if group_by == "Vendor Group" else None,
                account_ids=account_ids,
            )
            series_by_value.append((label, s))

        specs: list[tuple[str, list[QPointF], str, Qt.PenStyle, float]] = []
        all_values: list[float] = []
        for i, (label, s) in enumerate(series_by_value):
            color = SERIES_COLORS[i % len(SERIES_COLORS)]
            specs.append(
                (
                    f"{label} — Actual",
                    [QPointF(_to_msecs(d), v) for d, v in zip(s.bucket_starts, s.actual)],
                    color,
                    Qt.PenStyle.SolidLine,
                    2.0,
                )
            )
            all_values += s.actual
            if show_ma:
                specs.append(
                    (
                        f"{label} — Moving Avg",
                        [QPointF(_to_msecs(d), v) for d, v in zip(s.bucket_starts, s.actual_moving_avg)],
                        color,
                        Qt.PenStyle.DotLine,
                        2.5,
                    )
                )
                all_values += s.actual_moving_avg
            if show_forecast:
                specs.append(
                    (
                        f"{label} — Forecast",
                        [QPointF(_to_msecs(d), v) for d, v in zip(s.bucket_starts, s.forecast)],
                        color,
                        Qt.PenStyle.DashLine,
                        2.0,
                    )
                )
                all_values += s.forecast
        self._set_series(specs)

        bucket_starts = series_by_value[0][1].bucket_starts if series_by_value else []
        if bucket_starts:
            self.x_axis.setRange(
                QDateTime.fromMSecsSinceEpoch(int(_to_msecs(bucket_starts[0]))),
                QDateTime.fromMSecsSinceEpoch(int(_to_msecs(bucket_starts[-1]))),
            )
        y_max = max(all_values) if all_values else 0.0
        self.y_axis.setRange(0, max(y_max * 1.15, 10))

        self.table.setRowCount(0)
        for label, s in series_by_value:
            for period, actual, ma, forecast in zip(
                s.bucket_labels, s.actual, s.actual_moving_avg, s.forecast
            ):
                r = self.table.rowCount()
                self.table.insertRow(r)
                self.table.setItem(r, 0, QTableWidgetItem(label))
                self.table.setItem(r, 1, QTableWidgetItem(period))
                self.table.setItem(r, 2, self._money_item(actual))
                self.table.setItem(r, 3, self._money_item(ma))
                self.table.setItem(r, 4, self._money_item(forecast))
        self.table.resizeColumnsToContents()

        total_actual = sum(sum(s.actual) for _, s in series_by_value)
        total_forecast = sum(sum(s.forecast) for _, s in series_by_value)
        total_matched_tx = sum(s.matched_transaction_count for _, s in series_by_value)
        total_matched_items = len({i for _, s in series_by_value for i in s.matched_budget_item_ids})
        self.summary_label.setText(
            f"{len(series_by_value)} group(s), {total_matched_tx} matching transaction(s) — total actual "
            f"£{total_actual:,.2f}, total forecast £{total_forecast:,.2f} "
            f"({total_matched_items} matching budget item(s))."
        )
