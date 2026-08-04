import datetime as dt

import polars as pl
from PySide6.QtCharts import QChart, QChartView, QPieSeries
from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.forecast import monthly_budget_summary
from app.ui import theme

SECTION_BG = QColor(theme.SECTION_BG)
TOTAL_BG = QColor(theme.TOTAL_BG)

# Fixed categorical order (not cycled/reassigned as data changes) tuned for a
# dark chart surface - see the data-viz palette this app's charts follow.
CATEGORY_COLORS = [
    "#3987e5",
    "#008300",
    "#d55181",
    "#c98500",
    "#199e70",
    "#d95926",
    "#9085e9",
    "#e66767",
]
MAX_PIE_SLICES = 8


class BudgetViewScreen(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session

        outer = QHBoxLayout(self)

        left = QVBoxLayout()
        left.addWidget(QLabel("<h2>Budget</h2>"))
        left.addWidget(QLabel("Monthly-equivalent totals for every Budget Item active on the selected date."))

        controls = QHBoxLayout()
        controls.addWidget(QLabel("As of:"))
        self.as_of_edit = QDateEdit(QDate.currentDate())
        self.as_of_edit.setCalendarPopup(True)
        self.as_of_edit.dateChanged.connect(self.refresh)
        controls.addWidget(self.as_of_edit)
        controls.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        controls.addWidget(refresh_btn)
        left.addLayout(controls)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Category", "Monthly Amount"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        left.addWidget(self.table)

        outer.addLayout(left, 3)

        right = QVBoxLayout()
        right.addStretch()
        right.addWidget(self._build_expense_chart())
        self.expense_hover_label = self._build_hover_label()
        right.addWidget(self.expense_hover_label)
        right.addStretch()
        outer.addLayout(right, 2)

        self.refresh()

    # -- chart construction --------------------------------------------------

    def _build_hover_label(self) -> QLabel:
        label = QLabel("")
        label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        label.setMinimumHeight(18)
        return label

    def _build_expense_chart(self) -> QChartView:
        self.expense_series = QPieSeries()
        self.expense_series.setHoleSize(0.55)

        chart = QChart()
        chart.addSeries(self.expense_series)
        chart.setTitle("Expense Breakdown")
        chart.setTitleBrush(QColor(theme.TEXT))
        chart.setBackgroundBrush(QColor(theme.SURFACE))
        chart.setBackgroundPen(QColor(theme.BORDER))
        chart.setPlotAreaBackgroundVisible(False)
        chart.legend().setVisible(True)
        chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        chart.legend().setLabelColor(QColor(theme.TEXT_MUTED))
        self.expense_chart = chart

        view = QChartView(chart)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(260)
        view.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        return view

    # -- chart interaction -----------------------------------------------

    def _on_expense_slice_hovered(self, slice_, state: bool, text: str) -> None:
        slice_.setExploded(state)
        self.expense_hover_label.setText(text if state else "")

    # -- chart data --------------------------------------------------------

    def _update_expense_chart(self, section: pl.DataFrame, total: float) -> None:
        self.expense_series.clear()
        if section.height == 0 or total <= 0:
            return

        rows = sorted(section.iter_rows(named=True), key=lambda r: r["monthly_amount"], reverse=True)
        top, rest = rows[:MAX_PIE_SLICES], rows[MAX_PIE_SLICES:]

        for i, row in enumerate(top):
            category, amount = row["category"], row["monthly_amount"]
            slice_ = self.expense_series.append(category, amount)
            slice_.setBrush(QColor(CATEGORY_COLORS[i % len(CATEGORY_COLORS)]))
            slice_.setPen(QColor(theme.SURFACE))
            slice_.setLabelVisible(False)
            text = f"{category}: £{amount:,.0f} ({amount / total * 100:.0f}%)"
            slice_.hovered.connect(
                lambda state, s=slice_, t=text: self._on_expense_slice_hovered(s, state, t)
            )

        if rest:
            other_amount = sum(r["monthly_amount"] for r in rest)
            slice_ = self.expense_series.append(f"Other ({len(rest)})", other_amount)
            slice_.setBrush(QColor(theme.TEXT_MUTED))
            slice_.setPen(QColor(theme.SURFACE))
            slice_.setLabelVisible(False)
            text = f"Other: £{other_amount:,.0f} ({other_amount / total * 100:.0f}%)"
            slice_.hovered.connect(
                lambda state, s=slice_, t=text: self._on_expense_slice_hovered(s, state, t)
            )

    def _add_row(self, label: str, amount: float | None = None, bold: bool = False, bg: QColor | None = None):
        row = self.table.rowCount()
        self.table.insertRow(row)
        label_item = QTableWidgetItem(label)
        self.table.setItem(row, 0, label_item)
        amount_item = QTableWidgetItem(f"£{amount:,.2f}" if amount is not None else "")
        amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, 1, amount_item)
        if bold:
            for item in (label_item, amount_item):
                font = item.font()
                font.setBold(True)
                item.setFont(font)
        if bg is not None:
            label_item.setBackground(bg)
            amount_item.setBackground(bg)

    def refresh(self):
        as_of = dt.date(
            self.as_of_edit.date().year(), self.as_of_edit.date().month(), self.as_of_edit.date().day()
        )
        summary = monthly_budget_summary(self.session, as_of)

        self.table.setRowCount(0)
        total_income = 0.0
        total_expense = 0.0
        expense_section = pl.DataFrame(schema=summary.schema)
        for flow in ("Income", "Expense"):
            section = summary.filter(pl.col("flow_type") == flow)
            self._add_row(flow.upper(), bold=True, bg=SECTION_BG)
            subtotal = 0.0
            for row in section.sort("category").iter_rows(named=True):
                self._add_row(row["category"], row["monthly_amount"])
                subtotal += row["monthly_amount"]
            if section.height == 0:
                self._add_row("(none)")
            self._add_row(f"Total {flow}", subtotal, bold=True, bg=TOTAL_BG)
            if flow == "Income":
                total_income = subtotal
            else:
                total_expense = subtotal
                expense_section = section

        self._add_row("")
        self._add_row("Net Remaining", total_income - total_expense, bold=True, bg=TOTAL_BG)
        self.table.resizeColumnToContents(0)

        self._update_expense_chart(expense_section, total_expense)
