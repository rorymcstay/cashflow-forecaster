import datetime as dt

from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QPushButton
from sqlalchemy.orm import Session

from app import investments
from app.forecast import account_daily_forecast, account_run_rate
from app.models import Account, BudgetItem, UpcomingExpense
from app.ui import theme
from app.ui.crud_screen import CrudScreen
from app.ui.dialogs import AccountDialog

FORECAST_DAYS = 30

COLUMNS = [
    ("Name", lambda a: a.name),
    ("Current Balance", lambda a: f"£{a.current_balance:,.2f}", lambda a: a.current_balance),
    ("Balance As Of", lambda a: a.balance_as_of.strftime("%d %b %Y"), lambda a: a.balance_as_of),
    (
        "Net Monthly Flow",
        lambda a: _net_flow_label(a),
        lambda a: a.net_monthly_flow,
    ),
    (
        f"{FORECAST_DAYS}-Day Forecast",
        lambda a: f"£{a.forecast_balance_30d:,.2f}",
        lambda a: a.forecast_balance_30d,
        lambda a: theme.WARNING if a.forecast_balance_30d < 0 else theme.SUCCESS,
    ),
    (
        "Low Balance Warning",
        lambda a: f"£{a.low_balance_threshold:,.2f}" if a.low_balance_threshold is not None else "—",
        lambda a: a.low_balance_threshold if a.low_balance_threshold is not None else float("-inf"),
    ),
    (
        "Growth Rate",
        lambda a: f"{a.growth_rate:.2f}% APY" if a.growth_rate else "—",
        lambda a: a.growth_rate or 0.0,
    ),
    (
        "Holdings",
        lambda a: ", ".join(f"{h.ticker} x{h.quantity:g}" for h in a.holdings) if a.holdings else "—",
    ),
    (
        "Cash Position",
        lambda a: f"£{a.cash_position:,.2f}" if a.holdings else "—",
        lambda a: a.cash_position,
    ),
    (
        "Credit Card Autopay",
        lambda a: _cc_autopay_label(a),
    ),
]


def _net_flow_label(account: Account) -> str:
    net = account.net_monthly_flow
    sign = "+" if net >= 0 else "-"
    return f"{sign}£{abs(net):,.2f}/mo"


def _cc_autopay_label(account: Account) -> str:
    if not account.is_credit_card:
        return "—"
    if account.cc_payee_account is None or account.cc_payment_day is None:
        return "Credit card (autopay not set up)"
    payment = "in full" if account.cc_pay_in_full else f"£{account.cc_fixed_payment_amount or 0:,.2f}"
    return f"{payment} from {account.cc_payee_account.name} on day {account.cc_payment_day}"


def _forecast_balance(session: Session, account: Account, days: int = FORECAST_DAYS) -> float:
    today = dt.date.today()
    df = account_daily_forecast(session, account, today, today + dt.timedelta(days=days))
    if df.height == 0:
        return account.current_balance
    return float(df["balance"][-1])


def query_accounts(session: Session) -> list[Account]:
    accounts = session.query(Account).order_by(Account.name).all()
    for account in accounts:
        account.net_monthly_flow = account_run_rate(session, account)["avg_monthly_net"]
        account.forecast_balance_30d = _forecast_balance(session, account)
    return accounts


class AccountsScreen(CrudScreen):
    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(
            session, "Accounts", COLUMNS, query_accounts, AccountDialog, on_change=on_change, parent=parent
        )
        sync_row = QHBoxLayout()
        sync_btn = QPushButton("Sync Investment Prices")
        sync_btn.setToolTip(
            "Re-fetch live prices for every account with holdings and refresh its balance "
            "(cash position + market value) from them."
        )
        sync_btn.clicked.connect(self._sync_investment_prices)
        sync_row.addWidget(sync_btn)
        sync_row.addStretch()
        self.main_layout.addLayout(sync_row)

    def _sync_investment_prices(self):
        accounts = [a for a in self.session.query(Account).all() if a.holdings]
        if not accounts:
            QMessageBox.information(self, "No investment accounts", "No account has holdings to sync yet.")
            return
        failed = []
        for account in accounts:
            if investments.refresh_investment_value(self.session, account) is None:
                failed.append(account.name)
        self._notify_change()
        if failed:
            QMessageBox.warning(
                self,
                "Some prices unavailable",
                f"Synced {len(accounts) - len(failed)} of {len(accounts)} account(s). Couldn't fetch "
                f"prices for: {', '.join(failed)}.",
            )
        else:
            QMessageBox.information(self, "Synced", f"Refreshed {len(accounts)} investment account(s).")

    def delete_item(self):
        obj = self.selected_object()
        if obj is None:
            QMessageBox.information(self, "No selection", "Select a row to delete first.")
            return
        used_budget = self.session.query(BudgetItem).filter_by(account_id=obj.id).count()
        used_upcoming = self.session.query(UpcomingExpense).filter_by(account_id=obj.id).count()
        used_as_target = (
            self.session.query(BudgetItem).filter_by(target_account_id=obj.id).count()
            + self.session.query(UpcomingExpense).filter_by(target_account_id=obj.id).count()
        )
        used_as_cc_payee = self.session.query(Account).filter_by(cc_payee_account_id=obj.id).count()
        if used_budget or used_upcoming or used_as_target or used_as_cc_payee:
            QMessageBox.warning(
                self,
                "Account in use",
                f"Can't delete '{obj.name}' — it's used by {used_budget} budget item(s), "
                f"{used_upcoming} upcoming expense(s), {used_as_target} transfer(s) targeting it, "
                f"and {used_as_cc_payee} credit card(s) that pay from it. Reassign or delete those first.",
            )
            return
        super().delete_item()
