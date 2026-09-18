from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.budgets import (
    archive_budget,
    create_budget,
    delete_budget,
    get_active_budget,
    list_budgets,
    rename_budget,
    restore_budget,
    set_active_budget,
)
from app.models import Budget, BudgetStatus
from app.ui import theme


class BudgetsScreen(QWidget):
    """Manage named, switchable budgets. Exactly one is "active" at a time —
    every other screen/forecast reads only the active budget's items — so
    this screen is the one place to switch, branch (Save As), rename,
    archive/restore, or delete one."""

    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.on_change = on_change

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Budgets</h2>"))
        description = QLabel(
            "Exactly one budget is active at a time — every screen and forecast reads only its "
            "budget items. “Save Active As…” branches the active budget under a new name; "
            "archiving hides a budget from active use without deleting its history."
        )
        description.setWordWrap(True)
        description.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(description)

        btn_row = QHBoxLayout()
        new_btn = QPushButton("+ New Budget")
        new_btn.setObjectName("primaryButton")
        new_btn.clicked.connect(self._new_budget)
        save_as_btn = QPushButton("Save Active As…")
        save_as_btn.clicked.connect(self._save_as)
        btn_row.addWidget(new_btn)
        btn_row.addWidget(save_as_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Name", "Status", "Budget Items", "Created", ""])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        # Rows hold a QWidget of several QPushButtons (not plain text), which
        # Qt's default row height doesn't leave enough room for — without
        # this the buttons render squashed and their labels are unreadable.
        self.table.verticalHeader().setDefaultSectionSize(44)
        layout.addWidget(self.table)

        self.refresh()

    def refresh(self):
        active = get_active_budget(self.session)
        budgets = list_budgets(self.session)
        self.table.setRowCount(len(budgets))
        for row, budget in enumerate(budgets):
            is_active = budget.id == active.id
            self.table.setItem(row, 0, QTableWidgetItem(("★ " if is_active else "") + budget.name))
            self.table.setItem(row, 1, QTableWidgetItem(budget.status.value))
            self.table.setItem(row, 2, QTableWidgetItem(str(len(budget.budget_items))))
            self.table.setItem(row, 3, QTableWidgetItem(budget.created_at.strftime("%d %b %Y")))

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(4, 4, 4, 4)
            actions_layout.setSpacing(6)
            if not is_active:
                activate_btn = QPushButton("Activate")
                activate_btn.clicked.connect(lambda _checked=False, b=budget: self._activate(b))
                actions_layout.addWidget(activate_btn)
            rename_btn = QPushButton("Rename")
            rename_btn.clicked.connect(lambda _checked=False, b=budget: self._rename(b))
            actions_layout.addWidget(rename_btn)
            if budget.status == BudgetStatus.ACTIVE:
                archive_btn = QPushButton("Archive")
                archive_btn.clicked.connect(lambda _checked=False, b=budget: self._archive(b))
                actions_layout.addWidget(archive_btn)
            else:
                restore_btn = QPushButton("Restore")
                restore_btn.clicked.connect(lambda _checked=False, b=budget: self._restore(b))
                actions_layout.addWidget(restore_btn)
            if not is_active:
                delete_btn = QPushButton("Delete")
                delete_btn.clicked.connect(lambda _checked=False, b=budget: self._delete(b))
                actions_layout.addWidget(delete_btn)
            actions_layout.addStretch()
            self.table.setCellWidget(row, 4, actions)
        self.table.resizeColumnsToContents()
        self.table.resizeRowsToContents()

    def _notify_change(self):
        self.refresh()
        if self.on_change:
            self.on_change()

    def _name_in_use(self, name: str, ignore: Budget | None = None) -> bool:
        existing = self.session.query(Budget).filter(Budget.name == name).one_or_none()
        return existing is not None and existing is not ignore

    def _new_budget(self):
        name, ok = QInputDialog.getText(self, "New Budget", "Name:")
        name = name.strip()
        if not ok or not name:
            return
        if self._name_in_use(name):
            QMessageBox.warning(self, "Name in use", f"A budget named '{name}' already exists.")
            return
        create_budget(self.session, name)
        self.session.commit()
        self._notify_change()

    def _save_as(self):
        active = get_active_budget(self.session)
        name, ok = QInputDialog.getText(
            self,
            "Save Active Budget As",
            "New budget name:",
            QLineEdit.EchoMode.Normal,
            f"{active.name} copy",
        )
        name = name.strip()
        if not ok or not name:
            return
        if self._name_in_use(name):
            QMessageBox.warning(self, "Name in use", f"A budget named '{name}' already exists.")
            return
        create_budget(self.session, name, clone_from=active)
        self.session.commit()
        self._notify_change()

    def _activate(self, budget: Budget):
        set_active_budget(self.session, budget.id)
        self.session.commit()
        self._notify_change()

    def _rename(self, budget: Budget):
        name, ok = QInputDialog.getText(
            self, "Rename Budget", "Name:", QLineEdit.EchoMode.Normal, budget.name
        )
        name = name.strip()
        if not ok or not name or name == budget.name:
            return
        if self._name_in_use(name, ignore=budget):
            QMessageBox.warning(self, "Name in use", f"A budget named '{name}' already exists.")
            return
        rename_budget(budget, name)
        self.session.commit()
        self._notify_change()

    def _archive(self, budget: Budget):
        try:
            archive_budget(self.session, budget)
            self.session.commit()
        except ValueError as e:
            self.session.rollback()
            QMessageBox.warning(self, "Can't archive", str(e))
            return
        self._notify_change()

    def _restore(self, budget: Budget):
        restore_budget(budget)
        self.session.commit()
        self._notify_change()

    def _delete(self, budget: Budget):
        reply = QMessageBox.question(
            self,
            "Confirm delete",
            f"Delete '{budget.name}' and its {len(budget.budget_items)} budget item(s)? "
            "This can't be undone.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_budget(self.session, budget)
            self.session.commit()
        except ValueError as e:
            self.session.rollback()
            QMessageBox.warning(self, "Can't delete", str(e))
            return
        self._notify_change()
