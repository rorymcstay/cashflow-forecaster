from sqlalchemy.orm import Session

from app.models import UpcomingExpense
from app.ui.crud_screen import CrudScreen
from app.ui.dialogs import UpcomingExpenseDialog

COLUMNS = [
    ("Date", lambda u: u.date.strftime("%d %b %Y"), lambda u: u.date),
    ("Description", lambda u: u.description),
    ("Amount", lambda u: f"£{u.amount:,.2f}", lambda u: u.amount),
    ("Category", lambda u: u.category.name),
    ("Account", lambda u: u.account.name),
]


def query_upcoming_expenses(session: Session) -> list[UpcomingExpense]:
    return session.query(UpcomingExpense).order_by(UpcomingExpense.date).all()


class UpcomingExpensesScreen(CrudScreen):
    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(
            session,
            "Upcoming Expenses",
            COLUMNS,
            query_upcoming_expenses,
            UpcomingExpenseDialog,
            on_change=on_change,
            parent=parent,
        )
