import datetime as dt

import polars as pl
from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.forecast import (
    account_run_rate,
    investment_accounts_summary,
    low_balance_warnings,
    monthly_budget_summary,
    monthly_savings_amount,
)
from app.models import Account
from app.ui import theme
from app.ui.widgets import AccountMultiSelect

WARNING_HORIZON_DAYS = 90


def _to_pydate(qd: QDate) -> dt.date:
    return dt.date(qd.year(), qd.month(), qd.day())


def _money_item(value: float) -> QTableWidgetItem:
    item = QTableWidgetItem(f"£{value:,.2f}")
    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


class DashboardScreen(QWidget):
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Financial Health Dashboard</h2>"))

        controls = QHBoxLayout()
        controls.addWidget(QLabel("As of:"))
        self.as_of_edit = QDateEdit(QDate.currentDate())
        self.as_of_edit.setCalendarPopup(True)
        self.as_of_edit.dateChanged.connect(self.refresh)
        controls.addWidget(self.as_of_edit)

        controls.addWidget(QLabel("Exclude:"))
        self.exclude_select = AccountMultiSelect(label_mode="exclude")
        self.exclude_select.selectionChanged.connect(self.refresh)
        controls.addWidget(self.exclude_select)

        controls.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        controls.addWidget(refresh_btn)
        layout.addLayout(controls)

        tiles = QHBoxLayout()
        self.net_worth_tile, self.net_worth_value = self._build_tile("Net Worth")
        self.income_tile, self.income_value = self._build_tile("Monthly Income")
        self.expense_tile, self.expense_value = self._build_tile("Monthly Expense")
        self.savings_tile, self.savings_value = self._build_tile("Savings Rate")
        for tile in (self.net_worth_tile, self.income_tile, self.expense_tile, self.savings_tile):
            tiles.addWidget(tile)
        layout.addLayout(tiles)

        self.savings_detail_label = QLabel()
        self.savings_detail_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.savings_detail_label)

        layout.addWidget(QLabel("<h3>Investment Returns</h3>"))
        self.investments_table = self._build_table(
            ["Account", "Balance", "Annualised Return", "Est. Monthly Growth"]
        )
        layout.addWidget(self.investments_table)
        self.investments_empty_label = QLabel("No investment/growth accounts set up.")
        self.investments_empty_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.investments_empty_label)

        layout.addWidget(QLabel("<h3>Accounts At Risk</h3>"))
        self.risk_table = self._build_table(["Account", "Net Monthly Flow", "Status"])
        layout.addWidget(self.risk_table)
        self.risk_empty_label = QLabel("No accounts with a negative run rate or projected overdraft.")
        self.risk_empty_label.setStyleSheet(f"color: {theme.SUCCESS};")
        layout.addWidget(self.risk_empty_label)

        layout.addWidget(QLabel(f"<h3>Low Balance Warnings (next {WARNING_HORIZON_DAYS} days)</h3>"))
        self.warnings_label = QLabel()
        self.warnings_label.setWordWrap(True)
        layout.addWidget(self.warnings_label)

        layout.addStretch()

        self.reload_accounts()
        self.refresh()

    # -- construction helpers -----------------------------------------------

    def _build_tile(self, title: str) -> tuple[QFrame, QLabel]:
        frame = QFrame()
        frame.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        v = QVBoxLayout(frame)
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        value_label = QLabel("—")
        value_label.setStyleSheet(f"color: {theme.TEXT}; font-size: 22px; font-weight: 700;")
        v.addWidget(title_label)
        v.addWidget(value_label)
        return frame, value_label

    def _build_table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        return table

    # -- data ----------------------------------------------------------------

    def reload_accounts(self) -> None:
        self.exclude_select.set_accounts(
            self.session.query(Account).order_by(Account.name).all(), default_all_checked=False
        )

    def refresh(self) -> None:
        as_of = _to_pydate(self.as_of_edit.date())
        excluded = self.exclude_select.checked_ids()
        accounts = (
            self.session.query(Account).filter(Account.id.notin_(excluded)).order_by(Account.name).all()
        )

        net_worth = sum(a.current_balance for a in accounts)
        self.net_worth_value.setText(f"£{net_worth:,.0f}")

        summary = monthly_budget_summary(self.session, as_of, exclude_account_ids=excluded)
        income = summary.filter(pl.col("flow_type") == "Income")["monthly_amount"].sum()
        expense = summary.filter(pl.col("flow_type") == "Expense")["monthly_amount"].sum()
        net_remaining = income - expense
        savings_rate = (net_remaining / income) if income else 0.0

        self.income_value.setText(f"£{income:,.0f}")
        self.expense_value.setText(f"£{expense:,.0f}")
        self.savings_value.setText(f"{savings_rate * 100:.0f}%")
        savings_color = theme.SUCCESS if savings_rate >= 0 else theme.WARNING
        self.savings_value.setStyleSheet(f"color: {savings_color}; font-size: 22px; font-weight: 700;")

        invested = monthly_savings_amount(self.session, as_of, exclude_account_ids=excluded)
        self.savings_detail_label.setText(
            f"£{net_remaining:,.2f}/mo net remaining (income − expense), of which "
            f"£{invested:,.2f}/mo is being actively invested via recurring transfers."
        )

        self._refresh_investments(as_of, excluded)
        self._refresh_risk_table(accounts)
        self._refresh_warnings(as_of, accounts)

    def _refresh_investments(self, as_of: dt.date, excluded: list[int]) -> None:
        self.investments_table.setRowCount(0)
        rows = [
            r for r in investment_accounts_summary(self.session, as_of) if r["account_id"] not in excluded
        ]
        for row in rows:
            r = self.investments_table.rowCount()
            self.investments_table.insertRow(r)
            self.investments_table.setItem(r, 0, QTableWidgetItem(row["account"]))
            self.investments_table.setItem(r, 1, _money_item(row["balance"]))
            self.investments_table.setItem(r, 2, QTableWidgetItem(f"{row['annual_rate'] * 100:.2f}%"))
            self.investments_table.setItem(r, 3, _money_item(row["monthly_growth_estimate"]))
        for col in range(4):
            self.investments_table.resizeColumnToContents(col)
        self.investments_table.setVisible(bool(rows))
        self.investments_empty_label.setVisible(not rows)

    def _refresh_risk_table(self, accounts: list[Account]) -> None:
        self.risk_table.setRowCount(0)
        for account in accounts:
            result = account_run_rate(self.session, account)
            avg = result["avg_monthly_net"]
            # A credit card's balance is negative by design (it's debt), so
            # "goes negative" is meaningless for it — only a growing balance
            # (avg_monthly_net < 0, i.e. debt increasing) is actually a risk.
            overdrawn = None if account.is_credit_card else result["overdrawn_date"]
            if avg >= 0 and overdrawn is None:
                continue
            r = self.risk_table.rowCount()
            self.risk_table.insertRow(r)
            self.risk_table.setItem(r, 0, QTableWidgetItem(account.name))
            flow_item = _money_item(avg)
            if avg < 0:
                flow_item.setForeground(QColor(theme.WARNING))
            self.risk_table.setItem(r, 1, flow_item)
            status = (
                f"Projected overdrawn ~{overdrawn.strftime('%d %b %Y')}" if overdrawn else "Declining balance"
            )
            status_item = QTableWidgetItem(status)
            status_item.setForeground(QColor(theme.WARNING))
            self.risk_table.setItem(r, 2, status_item)
        for col in range(3):
            self.risk_table.resizeColumnToContents(col)
        has_risk = self.risk_table.rowCount() > 0
        self.risk_table.setVisible(has_risk)
        self.risk_empty_label.setVisible(not has_risk)

    def _refresh_warnings(self, as_of: dt.date, accounts: list[Account]) -> None:
        warnings = low_balance_warnings(
            self.session, as_of, as_of + dt.timedelta(days=WARNING_HORIZON_DAYS), accounts
        )
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
            self.warnings_label.setStyleSheet(f"color: {theme.WARNING}; font-weight: 600;")
        else:
            self.warnings_label.setText("No low-balance warnings in this range.")
            self.warnings_label.setStyleSheet(f"color: {theme.SUCCESS};")
