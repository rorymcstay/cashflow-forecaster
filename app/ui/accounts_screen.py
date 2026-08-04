from PySide6.QtWidgets import QMessageBox
from sqlalchemy.orm import Session

from app.models import Account, BudgetItem, UpcomingExpense
from app.ui.crud_screen import CrudScreen
from app.ui.dialogs import AccountDialog

COLUMNS = [
    ("Name", lambda a: a.name),
    ("Current Balance", lambda a: f"£{a.current_balance:,.2f}", lambda a: a.current_balance),
    ("Balance As Of", lambda a: a.balance_as_of.strftime("%d %b %Y"), lambda a: a.balance_as_of),
    (
        "Low Balance Warning",
        lambda a: f"£{a.low_balance_threshold:,.2f}" if a.low_balance_threshold is not None else "—",
        lambda a: a.low_balance_threshold if a.low_balance_threshold is not None else float("-inf"),
    ),
]


def query_accounts(session: Session) -> list[Account]:
    return session.query(Account).order_by(Account.name).all()


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
        if used_budget or used_upcoming:
            QMessageBox.warning(
                self,
                "Account in use",
                f"Can't delete '{obj.name}' — it's used by {used_budget} budget item(s) and "
                f"{used_upcoming} upcoming expense(s). Reassign or delete those first.",
            )
            return
        super().delete_item()
