from PySide6.QtWidgets import QMessageBox
from sqlalchemy.orm import Session

from app.forecast import account_run_rate
from app.models import Account, BudgetItem, UpcomingExpense
from app.ui.crud_screen import CrudScreen
from app.ui.dialogs import AccountDialog

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
        lambda a: ", ".join(h.ticker for h in a.holdings) if a.holdings else "—",
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


def query_accounts(session: Session) -> list[Account]:
    accounts = session.query(Account).order_by(Account.name).all()
    for account in accounts:
        account.net_monthly_flow = account_run_rate(session, account)["avg_monthly_net"]
    return accounts


class AccountsScreen(CrudScreen):
    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(
            session, "Accounts", COLUMNS, query_accounts, AccountDialog, on_change=on_change, parent=parent
        )

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
