import csv
import datetime as dt
from typing import ClassVar

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QScatterSeries, QValueAxis
from PySide6.QtCore import QDate, QDateTime, QPointF, Qt, QTime
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTableWidgetSelectionRange,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app import investments
from app.forecast import (
    account_daily_forecast,
    bucket_date_ranges,
    combined_daily_forecast,
    low_balance_warnings,
)
from app.models import Account
from app.ui import theme
from app.ui.widgets import AccountMultiSelect

WARNING_BG = QColor(theme.WARNING_BG)
HIGHLIGHT_FILL = QColor("#FFFFFF")

# Distinct line colors for the "split by account" chart view, cycled if there
# are more accounts than colors.
ACCOUNT_COLORS = [
    theme.ACCENT,
    theme.SUCCESS,
    theme.WARNING,
    "#F2B705",
    "#B45BEF",
    "#05C7F2",
    "#F2905B",
    theme.TEXT_MUTED,
]

CHART_FREQUENCIES = ["Daily", "Weekly", "Monthly"]


def _to_msecs(date: dt.date) -> float:
    return float(QDateTime(QDate(date.year, date.month, date.day), QTime(0, 0)).toMSecsSinceEpoch())


class CashflowForecastScreen(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self._table_dates: list[dt.date] = []
        self._bucket_row_ranges: list[tuple[int, int]] = []
        self._chart_dates: list[dt.date] = []
        self._chart_balances: list[float] = []
        self._split_mode_active = False

        outer_layout = QVBoxLayout(self)
        outer_layout.addWidget(QLabel("<h2>Cash Flow Forecast</h2>"))

        self.tabs = QTabWidget()
        outer_layout.addWidget(self.tabs)

        cashflow_tab = QWidget()
        layout = QVBoxLayout(cashflow_tab)
        self.tabs.addTab(cashflow_tab, "Cashflow")

        self.investments_tab = InvestmentsTab(self.session)
        self.tabs.addTab(self.investments_tab, "Investments")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        today = QDate.currentDate()
        month_start = QDate(today.year(), today.month(), 1)
        month_end = QDate(today.year(), today.month(), today.daysInMonth())

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Start:"))
        self.start_edit = QDateEdit(month_start)
        self.start_edit.setDisplayFormat("d MMM yyyy")
        self.start_edit.setCalendarPopup(True)
        self.start_edit.dateChanged.connect(self.refresh)
        controls.addWidget(self.start_edit)

        controls.addWidget(QLabel("End:"))
        self.end_edit = QDateEdit(month_end)
        self.end_edit.setDisplayFormat("d MMM yyyy")
        self.end_edit.setCalendarPopup(True)
        self.end_edit.dateChanged.connect(self.refresh)
        controls.addWidget(self.end_edit)

        controls.addWidget(QLabel("Account:"))
        self.account_select = AccountMultiSelect()
        self.account_select.selectionChanged.connect(self._on_account_changed)
        controls.addWidget(self.account_select)

        controls.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        controls.addWidget(refresh_btn)
        layout.addLayout(controls)

        controls2 = QHBoxLayout()
        controls2.addWidget(QLabel("Chart points:"))
        self.freq_combo = QComboBox()
        for f in CHART_FREQUENCIES:
            self.freq_combo.addItem(f, f)
        self.freq_combo.currentIndexChanged.connect(self.refresh)
        controls2.addWidget(self.freq_combo)

        self.split_check = QCheckBox("Split chart by account")
        self.split_check.toggled.connect(self.refresh)
        controls2.addWidget(self.split_check)

        self.rebase_check = QCheckBox("Rebase to 0 (change since start)")
        self.rebase_check.toggled.connect(self.refresh)
        controls2.addWidget(self.rebase_check)

        controls2.addStretch()
        copy_btn = QPushButton("Copy Table")
        copy_btn.clicked.connect(self._copy_table)
        controls2.addWidget(copy_btn)

        export_btn = QPushButton("Export CSV…")
        export_btn.clicked.connect(self._export_csv)
        controls2.addWidget(export_btn)
        layout.addLayout(controls2)

        self.warnings_label = QLabel()
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setStyleSheet(f"color: {theme.WARNING}; font-weight: 600;")
        layout.addWidget(self.warnings_label)

        layout.addWidget(self._build_chart())

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Forecast In", "Forecast Out", "Net", "Balance", "Details"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        layout.addWidget(self.table)

        self.reload_accounts()
        self.refresh()

    # -- chart -----------------------------------------------------------

    def _build_chart(self) -> QChartView:
        self.chart = QChart()
        self.chart.legend().hide()
        self.chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        self.chart.legend().setLabelColor(QColor(theme.TEXT_MUTED))
        self.chart.setTitle("Balance")
        self.chart.setTitleBrush(QColor(theme.TEXT))
        self.chart.setBackgroundBrush(QColor(theme.SURFACE))
        self.chart.setBackgroundPen(QColor(theme.BORDER))
        self.chart.setPlotAreaBackgroundVisible(False)

        self._balance_series_list: list[QLineSeries] = []

        self.zero_series = QLineSeries()
        zero_pen = QPen(QColor(theme.TEXT_MUTED))
        zero_pen.setWidthF(1.0)
        zero_pen.setStyle(Qt.PenStyle.DotLine)
        self.zero_series.setPen(zero_pen)
        self.chart.addSeries(self.zero_series)

        self.threshold_series = QLineSeries()
        threshold_pen = QPen(QColor(theme.WARNING))
        threshold_pen.setWidthF(1.5)
        threshold_pen.setStyle(Qt.PenStyle.DashLine)
        self.threshold_series.setPen(threshold_pen)
        self.chart.addSeries(self.threshold_series)

        self.highlight_series = QScatterSeries()
        self.highlight_series.setMarkerSize(14.0)
        self.highlight_series.setColor(HIGHLIGHT_FILL)
        self.highlight_series.setBorderColor(QColor(theme.ACCENT))
        self.chart.addSeries(self.highlight_series)

        self.x_axis = QDateTimeAxis()
        self.x_axis.setFormat("d MMM")
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

        for series in (self.zero_series, self.threshold_series, self.highlight_series):
            series.attachAxis(self.x_axis)
            series.attachAxis(self.y_axis)

        chart_view = QChartView(self.chart)
        chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        chart_view.setMinimumHeight(230)
        chart_view.setMaximumHeight(280)
        chart_view.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        return chart_view

    def _set_balance_series(self, series_specs: list[tuple[str, list[QPointF], str]]) -> None:
        """Replace the chart's balance line(s). series_specs is a list of
        (name, points, color_hex); a single unnamed series hides the legend,
        multiple named ones (split-by-account) show it."""
        for series in self._balance_series_list:
            self.chart.removeSeries(series)
        self._balance_series_list = []

        for name, points, color in series_specs:
            series = QLineSeries()
            series.setName(name)
            pen = QPen(QColor(color))
            pen.setWidthF(2.5)
            series.setPen(pen)
            series.replace(points)
            series.clicked.connect(self._on_chart_point_clicked)
            self.chart.addSeries(series)
            series.attachAxis(self.x_axis)
            series.attachAxis(self.y_axis)
            self._balance_series_list.append(series)

        self.chart.legend().setVisible(len(series_specs) > 1)

    def _update_chart(
        self,
        series_specs: list[tuple[str, list[QPointF], str]],
        chart_dates: list[dt.date],
        chart_balances: list[float],
        threshold: float | None,
    ) -> None:
        self._chart_dates = chart_dates
        self._chart_balances = chart_balances

        self._set_balance_series(series_specs)
        self.highlight_series.clear()

        all_points = [p for _, points, _ in series_specs for p in points]
        if not all_points:
            self.threshold_series.clear()
            self.zero_series.clear()
            return

        xs = [p.x() for p in all_points]
        ys = [p.y() for p in all_points]
        self.x_axis.setRange(
            QDateTime.fromMSecsSinceEpoch(int(min(xs))), QDateTime.fromMSecsSinceEpoch(int(max(xs)))
        )

        y_min, y_max = min(ys), max(ys)
        y_min = min(y_min, 0.0)
        y_max = max(y_max, 0.0)
        if threshold is not None:
            y_min = min(y_min, threshold)
            y_max = max(y_max, threshold)
        pad = max((y_max - y_min) * 0.12, 10)
        self.y_axis.setRange(y_min - pad, y_max + pad)

        self.zero_series.replace([QPointF(min(xs), 0.0), QPointF(max(xs), 0.0)])

        if threshold is not None:
            self.threshold_series.replace([QPointF(min(xs), threshold), QPointF(max(xs), threshold)])
        else:
            self.threshold_series.clear()

    def _bucket_index_for_row(self, row: int) -> int | None:
        for i, (start, end) in enumerate(self._bucket_row_ranges):
            if start <= row <= end:
                return i
        return None

    def _on_table_selection_changed(self) -> None:
        row = self.table.currentRow()
        if self._split_mode_active or row < 0 or row >= len(self._table_dates):
            self.highlight_series.clear()
            return
        bucket_idx = self._bucket_index_for_row(row)
        if bucket_idx is None:
            self.highlight_series.clear()
            return
        x = _to_msecs(self._chart_dates[bucket_idx])
        y = self._chart_balances[bucket_idx]
        self.highlight_series.replace([QPointF(x, y)])

    def _on_chart_point_clicked(self, point: QPointF) -> None:
        if not self._chart_dates or not self._bucket_row_ranges:
            return
        clicked_qdate = QDateTime.fromMSecsSinceEpoch(int(point.x())).date()
        clicked_date = dt.date(clicked_qdate.year(), clicked_qdate.month(), clicked_qdate.day())
        bucket_idx = min(
            range(len(self._chart_dates)), key=lambda i: abs((self._chart_dates[i] - clicked_date).days)
        )
        start_row, end_row = self._bucket_row_ranges[bucket_idx]

        self.table.clearSelection()
        self.table.setCurrentCell(start_row, 0)
        self.table.setRangeSelected(
            QTableWidgetSelectionRange(start_row, 0, end_row, self.table.columnCount() - 1), True
        )
        self.table.scrollToItem(self.table.item(start_row, 0))

    # -- data --------------------------------------------------------------

    def reload_accounts(self):
        accounts = self.session.query(Account).order_by(Account.name).all()
        self.account_select.set_accounts(accounts)
        self._update_split_check_enabled()
        self.investments_tab.refresh()

    def _on_tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.investments_tab:
            self.investments_tab.refresh()

    def _selected_accounts(self) -> list[Account]:
        """Accounts to forecast for, honoring the multi-select."""
        checked = self.account_select.checked_ids()
        return self.session.query(Account).filter(Account.id.in_(checked)).order_by(Account.name).all()

    def _update_split_check_enabled(self) -> None:
        self.split_check.setEnabled(len(self._selected_accounts()) != 1)

    def _on_account_changed(self):
        self._update_split_check_enabled()
        self.refresh()

    def _money_item(self, value: float) -> QTableWidgetItem:
        item = QTableWidgetItem(f"£{value:,.2f}")
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _table_to_rows(self) -> list[list[str]]:
        headers = [self.table.horizontalHeaderItem(c).text() for c in range(self.table.columnCount())]
        rows = [headers]
        for r in range(self.table.rowCount()):
            rows.append(
                [
                    self.table.item(r, c).text() if self.table.item(r, c) else ""
                    for c in range(self.table.columnCount())
                ]
            )
        return rows

    def _copy_table(self):
        rows = self._table_to_rows()
        QApplication.clipboard().setText("\n".join("\t".join(row) for row in rows))

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Cashflow", "cashflow.csv", "CSV Files (*.csv)")
        if not path:
            return
        with open(path, "w", newline="") as f:
            csv.writer(f).writerows(self._table_to_rows())

    def refresh(self):
        sd, ed = self.start_edit.date(), self.end_edit.date()
        range_start = dt.date(sd.year(), sd.month(), sd.day())
        range_end = dt.date(ed.year(), ed.month(), ed.day())

        if range_end < range_start:
            self.table.setRowCount(0)
            self.warnings_label.setText("End date is before start date.")
            self._update_chart([], [], [], None)
            return

        selected_accounts = self._selected_accounts()
        single_account = len(selected_accounts) == 1
        self._split_mode_active = not single_account and self.split_check.isChecked()
        account = None
        if not selected_accounts:
            self.table.setRowCount(0)
            self.warnings_label.setText("Select at least one account.")
            self._update_chart([], [], [], None)
            return
        if single_account:
            account = selected_accounts[0]
            df = account_daily_forecast(self.session, account, range_start, range_end)
        else:
            df = combined_daily_forecast(self.session, range_start, range_end, accounts=selected_accounts)

        self.table.setRowCount(0)
        if df.height == 0:
            self.warnings_label.setText(
                "No data for this range — the account's Balance As Of date is after the selected range."
            )
            self._update_chart([], [], [], None)
            return

        dates: list[dt.date] = []
        balances: list[float] = []
        for row in df.iter_rows(named=True):
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(row["date"].strftime("%a %d %b %Y")))
            self.table.setItem(r, 1, self._money_item(row["in"]))
            self.table.setItem(r, 2, self._money_item(row["out"]))
            self.table.setItem(r, 3, self._money_item(row["net"]))
            self.table.setItem(r, 4, self._money_item(row["balance"]))
            self.table.setItem(r, 5, QTableWidgetItem(row.get("details") or ""))
            if single_account and row.get("below_threshold"):
                for c in range(6):
                    self.table.item(r, c).setBackground(WARNING_BG)
            dates.append(row["date"])
            balances.append(row["balance"])

        for col in range(5):
            self.table.resizeColumnToContents(col)

        freq = self.freq_combo.currentData() or "Daily"
        bucket_ranges = bucket_date_ranges(dates, freq)
        chart_dates = [dates[end] for _, end in bucket_ranges]
        chart_balances = [balances[end] for _, end in bucket_ranges]
        rebase = self.rebase_check.isChecked()
        threshold = account.low_balance_threshold if single_account and account else None

        if self._split_mode_active:
            series_specs = []
            for i, acc in enumerate(selected_accounts):
                if acc.name not in df.columns:
                    continue
                col = df[acc.name].to_list()
                base = col[0] if rebase else 0.0
                points = [QPointF(_to_msecs(dates[end]), col[end] - base) for _, end in bucket_ranges]
                series_specs.append((acc.name, points, ACCOUNT_COLORS[i % len(ACCOUNT_COLORS)]))
        else:
            if rebase:
                base = balances[0]
                chart_balances = [b - base for b in chart_balances]
                if threshold is not None:
                    threshold -= base
            points = [QPointF(_to_msecs(d), b) for d, b in zip(chart_dates, chart_balances)]
            series_specs = [("Balance", points, theme.ACCENT)]

        self._table_dates = dates
        self._bucket_row_ranges = bucket_ranges

        self.chart.setTitle("Change Since Start" if rebase else "Balance")
        self._update_chart(series_specs, chart_dates, chart_balances, threshold)

        warnings = low_balance_warnings(self.session, range_start, range_end, accounts=selected_accounts)
        if warnings:
            shown = warnings[:8]
            lines = [
                f"⚠ {w['account']} drops below £{w['threshold']:,.2f} on "
                f"{w['date'].strftime('%d %b %Y')} (forecast £{w['balance']:,.2f})"
                for w in shown
            ]
            if len(warnings) > len(shown):
                lines.append(f"…and {len(warnings) - len(shown)} more in this range")
            self.warnings_label.setText("\n".join(lines))
        else:
            self.warnings_label.setText("")


class InvestmentsTab(QWidget):
    """Read-only view of every investment account's holdings, priced live:
    quantity, cost basis (if an average_price was ever recorded), current
    market value, and unrealized gain/loss — the actual "did this make
    money" report, as opposed to the Cashflow tab's forward-looking
    market-return projection."""

    COLUMNS: ClassVar[list[str]] = [
        "Account",
        "Ticker",
        "Quantity",
        "Avg Price",
        "Current Price",
        "Cost Basis",
        "Market Value",
        "Unrealized G/L",
        "G/L %",
    ]

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        sync_btn = QPushButton("Sync Investment Prices")
        sync_btn.clicked.connect(self._sync_and_refresh)
        top_row.addWidget(sync_btn)
        top_row.addStretch()
        layout.addLayout(top_row)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.summary_label)

        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        self.placeholder_label = QLabel(
            "No investment accounts yet — add holdings to an account on the Accounts tab."
        )
        self.placeholder_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.placeholder_label)

        self.refresh()

    def _money_item(self, value: float | None) -> QTableWidgetItem:
        item = QTableWidgetItem(f"£{value:,.2f}" if value is not None else "—")
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _sync_and_refresh(self) -> None:
        accounts = [a for a in self.session.query(Account).all() if a.holdings]
        if not accounts:
            QMessageBox.information(self, "No investment accounts", "No account has holdings to sync yet.")
            return
        failed = [a.name for a in accounts if investments.refresh_investment_value(self.session, a) is None]
        self.refresh()
        if failed:
            QMessageBox.warning(
                self,
                "Some prices unavailable",
                f"Synced {len(accounts) - len(failed)} of {len(accounts)}. Couldn't fetch prices "
                f"for: {', '.join(failed)}.",
            )

    def refresh(self) -> None:
        self.table.setRowCount(0)
        accounts = [a for a in self.session.query(Account).order_by(Account.name).all() if a.share_quantities]
        self.placeholder_label.setVisible(not accounts)
        self.table.setVisible(bool(accounts))

        total_cash = sum(a.cash_position for a in accounts)
        total_balance = sum(a.current_balance for a in accounts)
        total_cost_basis = 0.0
        total_gain = 0.0
        any_cost_basis = False

        for account in accounts:
            for h in investments.holdings_detail(account):
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem(account.name))
                self.table.setItem(row, 1, QTableWidgetItem(h["ticker"]))
                self.table.setItem(row, 2, QTableWidgetItem(f"{h['quantity']:g}"))
                self.table.setItem(row, 3, self._money_item(h["average_price"]))
                self.table.setItem(row, 4, self._money_item(h["price"]))
                self.table.setItem(row, 5, self._money_item(h["cost_basis"]))
                self.table.setItem(row, 6, self._money_item(h["value"]))
                gain_item = self._money_item(h["unrealized_gain"])
                if h["unrealized_gain"] is not None:
                    gain_item.setForeground(
                        QColor(theme.SUCCESS if h["unrealized_gain"] >= 0 else theme.WARNING)
                    )
                self.table.setItem(row, 7, gain_item)
                pct_text = (
                    f"{h['unrealized_gain_pct'] * 100:+.1f}%" if h["unrealized_gain_pct"] is not None else "—"
                )
                pct_item = QTableWidgetItem(pct_text)
                pct_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, 8, pct_item)

                if h["cost_basis"] is not None:
                    total_cost_basis += h["cost_basis"]
                    any_cost_basis = True
                if h["unrealized_gain"] is not None:
                    total_gain += h["unrealized_gain"]

        for col in range(len(self.COLUMNS) - 1):
            self.table.resizeColumnToContents(col)

        lines = [f"Cash: £{total_cash:,.2f}    Total balance: £{total_balance:,.2f}"]
        if any_cost_basis:
            gain_pct = f" ({total_gain / total_cost_basis * 100:+.1f}%)" if total_cost_basis else ""
            lines.append(
                f"Unrealized gain/loss (priced holdings with an avg price set): £{total_gain:,.2f}{gain_pct}"
            )
        else:
            lines.append("Set an avg price on a holding (Accounts tab) to see unrealized gain/loss here.")
        self.summary_label.setText("\n".join(lines))
