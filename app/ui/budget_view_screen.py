import datetime as dt

import polars as pl
from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDateEdit, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from sqlalchemy.orm import Session

from app.forecast import monthly_budget_summary
from app.ui import theme

SECTION_BG = QColor(theme.SECTION_BG)
TOTAL_BG = QColor(theme.TOTAL_BG)


class BudgetViewScreen(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Budget</h2>"))
        layout.addWidget(QLabel("Monthly-equivalent totals for every Budget Item active on the selected date."))

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
        layout.addLayout(controls)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Category", "Monthly Amount"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        self.refresh()

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
        as_of = dt.date(self.as_of_edit.date().year(), self.as_of_edit.date().month(), self.as_of_edit.date().day())
        summary = monthly_budget_summary(self.session, as_of)

        self.table.setRowCount(0)
        total_income = 0.0
        total_expense = 0.0
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

        self._add_row("")
        self._add_row("Net Remaining", total_income - total_expense, bold=True, bg=TOTAL_BG)
        self.table.resizeColumnToContents(0)
