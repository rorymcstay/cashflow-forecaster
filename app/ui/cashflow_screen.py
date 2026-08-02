import calendar
import datetime as dt

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QScatterSeries, QValueAxis
from PySide6.QtCore import QDate, QDateTime, QPointF, Qt, QTime
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from sqlalchemy.orm import Session

from app.forecast import account_daily_forecast, combined_daily_forecast, low_balance_warnings
from app.models import Account
from app.ui import theme

WARNING_BG = QColor(theme.WARNING_BG)
HIGHLIGHT_FILL = QColor("#FFFFFF")


def _to_msecs(date: dt.date) -> float:
    return float(QDateTime(QDate(date.year, date.month, date.day), QTime(0, 0)).toMSecsSinceEpoch())


class CashflowForecastScreen(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self._chart_dates: list[dt.date] = []
        self._chart_balances: list[float] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Cash Flow Forecast</h2>"))

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Month:"))
        self.month_edit = QDateEdit(QDate.currentDate())
        self.month_edit.setDisplayFormat("MMMM yyyy")
        self.month_edit.setCalendarPopup(True)
        self.month_edit.dateChanged.connect(self.refresh)
        controls.addWidget(self.month_edit)

        controls.addWidget(QLabel("Account:"))
        self.account_combo = QComboBox()
        self.account_combo.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.account_combo)

        controls.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        controls.addWidget(refresh_btn)
        layout.addLayout(controls)

        self.warnings_label = QLabel()
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setStyleSheet(f"color: {theme.WARNING}; font-weight: 600;")
        layout.addWidget(self.warnings_label)

        layout.addWidget(self._build_chart())

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Forecast In", "Forecast Out", "Net", "Balance", "Details"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        layout.addWidget(self.table)

        self.reload_accounts()
        self.refresh()

    # -- chart -----------------------------------------------------------

    def _build_chart(self) -> QChartView:
        self.chart = QChart()
        self.chart.legend().hide()
        self.chart.setTitle("Balance")
        self.chart.setTitleBrush(QColor(theme.TEXT))
        self.chart.setBackgroundBrush(QColor(theme.SURFACE))
        self.chart.setBackgroundPen(QColor(theme.BORDER))
        self.chart.setPlotAreaBackgroundVisible(False)

        self.balance_series = QLineSeries()
        balance_pen = QPen(QColor(theme.ACCENT))
        balance_pen.setWidthF(2.5)
        self.balance_series.setPen(balance_pen)
        self.balance_series.clicked.connect(self._on_chart_point_clicked)
        self.chart.addSeries(self.balance_series)

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

        for series in (self.balance_series, self.threshold_series, self.highlight_series):
            series.attachAxis(self.x_axis)
            series.attachAxis(self.y_axis)

        chart_view = QChartView(self.chart)
        chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        chart_view.setMinimumHeight(230)
        chart_view.setMaximumHeight(280)
        chart_view.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;")
        return chart_view

    def _update_chart(self, dates: list[dt.date], balances: list[float],
                       threshold: float | None) -> None:
        self._chart_dates = dates
        self._chart_balances = balances

        points = [QPointF(_to_msecs(d), bal) for d, bal in zip(dates, balances)]
        self.balance_series.replace(points)
        self.highlight_series.clear()

        if not points:
            self.threshold_series.clear()
            return

        xs = [p.x() for p in points]
        ys = [p.y() for p in points]
        self.x_axis.setRange(QDateTime.fromMSecsSinceEpoch(int(min(xs))),
                              QDateTime.fromMSecsSinceEpoch(int(max(xs))))

        y_min, y_max = min(ys), max(ys)
        if threshold is not None:
            y_min = min(y_min, threshold)
            y_max = max(y_max, threshold)
        pad = max((y_max - y_min) * 0.12, 10)
        self.y_axis.setRange(y_min - pad, y_max + pad)

        if threshold is not None:
            self.threshold_series.replace([QPointF(xs[0], threshold), QPointF(xs[-1], threshold)])
        else:
            self.threshold_series.clear()

    def _on_table_selection_changed(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._chart_dates):
            self.highlight_series.clear()
            return
        x = _to_msecs(self._chart_dates[row])
        y = self._chart_balances[row]
        self.highlight_series.replace([QPointF(x, y)])

    def _on_chart_point_clicked(self, point: QPointF) -> None:
        if not self._chart_dates:
            return
        clicked_qdate = QDateTime.fromMSecsSinceEpoch(int(point.x())).date()
        clicked_date = dt.date(clicked_qdate.year(), clicked_qdate.month(), clicked_qdate.day())
        closest_row = min(range(len(self._chart_dates)),
                           key=lambda i: abs((self._chart_dates[i] - clicked_date).days))
        self.table.selectRow(closest_row)
        self.table.scrollToItem(self.table.item(closest_row, 0))

    # -- data --------------------------------------------------------------

    def reload_accounts(self):
        current = self.account_combo.currentData() if self.account_combo.count() else None
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        self.account_combo.addItem("All Accounts (combined)", None)
        for account in self.session.query(Account).order_by(Account.name).all():
            self.account_combo.addItem(account.name, account.id)
        idx = self.account_combo.findData(current)
        self.account_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.account_combo.blockSignals(False)

    def _money_item(self, value: float) -> QTableWidgetItem:
        item = QTableWidgetItem(f"£{value:,.2f}")
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return item

    def refresh(self):
        qd = self.month_edit.date()
        month_start = dt.date(qd.year(), qd.month(), 1)
        month_end = dt.date(qd.year(), qd.month(), calendar.monthrange(qd.year(), qd.month())[1])

        account_id = self.account_combo.currentData()
        single_account = account_id is not None
        account = None
        if single_account:
            account = self.session.get(Account, account_id)
            df = account_daily_forecast(self.session, account, month_start, month_end)
        else:
            df = combined_daily_forecast(self.session, month_start, month_end)

        self.table.setRowCount(0)
        if df.height == 0:
            self.warnings_label.setText(
                "No data for this month — the account's Balance As Of date is after the selected month.")
            self._update_chart([], [], None)
            return

        dates: list[dt.date] = []
        balances: list[float] = []
        for row in df.iter_rows(named=True):
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(row["date"].strftime("%a %d %b")))
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

        threshold = account.low_balance_threshold if single_account and account else None
        self._update_chart(dates, balances, threshold)

        warnings = low_balance_warnings(self.session, month_start, month_end)
        if warnings:
            shown = warnings[:8]
            lines = [
                f"⚠ {w['account']} drops below £{w['threshold']:,.2f} on "
                f"{w['date'].strftime('%d %b')} (forecast £{w['balance']:,.2f})"
                for w in shown
            ]
            if len(warnings) > len(shown):
                lines.append(f"…and {len(warnings) - len(shown)} more this month")
            self.warnings_label.setText("\n".join(lines))
        else:
            self.warnings_label.setText("")
