import datetime as dt

from sqlalchemy.orm import Session

from app.models import BudgetItem
from app.ui.crud_screen import CrudScreen
from app.ui.dialogs import BudgetItemDialog

COLUMNS = [
    ("Description", lambda b: b.description),
    ("Category", lambda b: b.category.name),
    ("Account", lambda b: b.account.name),
    ("Amount", lambda b: f"£{b.amount:,.2f}", lambda b: b.amount),
    ("Type", lambda b: b.flow_type.value),
    ("Frequency", lambda b: b.frequency.value),
    ("Effective From", lambda b: b.effective_from.strftime("%d %b %Y"), lambda b: b.effective_from),
    (
        "Effective Until",
        lambda b: b.effective_until.strftime("%d %b %Y") if b.effective_until else "—",
        lambda b: b.effective_until or dt.date.max,
    ),
    ("Notes", lambda b: b.notes or ""),
]


def query_budget_items(session: Session) -> list[BudgetItem]:
    return session.query(BudgetItem).order_by(BudgetItem.effective_from).all()


class BudgetItemsScreen(CrudScreen):
    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(
            session,
            "Budget Items",
            COLUMNS,
            query_budget_items,
            BudgetItemDialog,
            on_change=on_change,
            parent=parent,
        )
