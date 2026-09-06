from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton
from sqlalchemy.orm import Session

from app.models import UpcomingExpense, UpcomingExpenseStatus
from app.ui.crud_screen import CrudScreen
from app.ui.dialogs import UpcomingExpenseDialog

COLUMNS = [
    ("Date", lambda u: u.date.strftime("%d %b %Y"), lambda u: u.date),
    ("Description", lambda u: u.description),
    ("Amount", lambda u: f"£{u.amount:,.2f}", lambda u: u.amount),
    ("Type", lambda u: u.flow_type.value),
    ("Status", lambda u: u.status.value),
    ("Category", lambda u: u.category.name),
    ("Account", lambda u: u.account.name),
    ("Target Account", lambda u: u.target_account.name if u.target_account else "—"),
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

        review_row = QHBoxLayout()
        review_row.addWidget(
            QLabel("Needs Review items: edit (double-click) to reschedule, or archive them here —")
        )
        archive_btn = QPushButton("Archive Selected")
        archive_btn.clicked.connect(self._archive_selected)
        review_row.addWidget(archive_btn)
        review_row.addStretch()
        self.layout().addLayout(review_row)

    def _archive_selected(self):
        obj = self.selected_object()
        if obj is None:
            QMessageBox.information(self, "No selection", "Select a row to archive first.")
            return
        obj.status = UpcomingExpenseStatus.ARCHIVED
        self.session.commit()
        self._notify_change()
