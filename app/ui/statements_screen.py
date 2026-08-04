import datetime as dt
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.models import Account, BudgetSuggestion, Statement, SuggestionStatus, Transaction
from app.statement_import import (
    accept_suggestion,
    budget_vs_actual_report,
    import_statement,
    reject_suggestion,
)
from app.statements import extract_csv_transactions
from app.ui import theme
from app.ui.dialogs import TransactionCategoryDialog

SECTION_BG = QColor(theme.SECTION_BG)
TOTAL_BG = QColor(theme.TOTAL_BG)
SUCCESS = QColor(theme.SUCCESS)
WARNING = QColor(theme.WARNING)


def _to_pydate(qd: QDate) -> dt.date:
    return dt.date(qd.year(), qd.month(), qd.day())


def _to_qdate(d: dt.date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _money(value: float) -> QTableWidgetItem:
    item = QTableWidgetItem(f"£{value:,.2f}")
    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


class StatementsScreen(QWidget):
    """Import bank/card statements, review the classified transactions and
    budget-vs-actual report for that billing period, and accept/reject the
    budget suggestions generated from transaction history.

    PDF statements aren't handled here — their layouts vary too much for one
    parser (see app/statements.py's read_pdf_text), so those still go through
    chat: read_pdf_statement -> transcribe -> the import_statement MCP tool.
    This screen covers the CSV happy path directly, plus reviewing/deciding
    on anything imported either way.
    """

    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.on_change = on_change
        self._pending_transactions: list[dict] = []
        self._selected_statement: Statement | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Statements</h2>"))

        layout.addWidget(self._build_import_panel())

        layout.addWidget(QLabel("<b>Imported Statements</b>"))
        self.statements_table = QTableWidget()
        self.statements_table.setColumnCount(5)
        self.statements_table.setHorizontalHeaderLabels(
            ["Account", "Period", "Imported", "Transactions", "Net"]
        )
        self.statements_table.horizontalHeader().setStretchLastSection(True)
        self.statements_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.statements_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.statements_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.statements_table.verticalHeader().setVisible(False)
        self.statements_table.setMaximumHeight(160)
        self.statements_table.itemSelectionChanged.connect(self._on_statement_selected)
        layout.addWidget(self.statements_table)

        delete_row = QHBoxLayout()
        delete_row.addStretch()
        delete_statement_btn = QPushButton("Delete Statement")
        delete_statement_btn.clicked.connect(self._delete_statement)
        delete_row.addWidget(delete_statement_btn)
        layout.addLayout(delete_row)

        layout.addWidget(self._build_detail_panel())
        layout.addWidget(self._build_suggestions_panel())

        self.reload_accounts()
        self.refresh()

    # -- import panel --------------------------------------------------

    def _build_import_panel(self) -> QWidget:
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Account:"))
        self.account_combo = QComboBox()
        row.addWidget(self.account_combo)

        self.file_btn = QPushButton("Choose CSV File…")
        self.file_btn.clicked.connect(self._choose_file)
        row.addWidget(self.file_btn)

        self.file_label = QLabel("No file chosen")
        self.file_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        row.addWidget(self.file_label, stretch=1)

        row.addWidget(QLabel("Period:"))
        self.start_edit = QDateEdit(QDate.currentDate())
        self.start_edit.setCalendarPopup(True)
        row.addWidget(self.start_edit)
        row.addWidget(QLabel("to"))
        self.end_edit = QDateEdit(QDate.currentDate())
        self.end_edit.setCalendarPopup(True)
        row.addWidget(self.end_edit)

        self.import_btn = QPushButton("Import")
        self.import_btn.setObjectName("primaryButton")
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._do_import)
        row.addWidget(self.import_btn)
        outer.addLayout(row)

        note = QLabel(
            "PDF statements aren't parsed here — their layouts vary too much for one parser. "
            "Read them via chat (read_pdf_statement) and import with the import_statement tool instead."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        outer.addWidget(note)
        return panel

    def _choose_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Choose CSV Statement", "", "CSV Files (*.csv)")
        if not file_path:
            return
        try:
            transactions = extract_csv_transactions(file_path)
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't read file", str(exc))
            return
        if not transactions:
            QMessageBox.warning(self, "No transactions found", "That file didn't contain any transactions.")
            return

        self._pending_transactions = transactions
        self.file_label.setText(Path(file_path).name)
        dates = [dt.date.fromisoformat(t["date"]) for t in transactions]
        self.start_edit.setDate(_to_qdate(min(dates)))
        self.end_edit.setDate(_to_qdate(max(dates)))
        self.import_btn.setEnabled(True)

    def _do_import(self):
        if not self._pending_transactions:
            return
        account_id = self.account_combo.currentData()
        if account_id is None:
            QMessageBox.warning(self, "No account", "Add an account first, on the Accounts tab.")
            return
        account = self.session.get(Account, account_id)
        period_start = _to_pydate(self.start_edit.date())
        period_end = _to_pydate(self.end_edit.date())

        try:
            statement = import_statement(
                self.session,
                account,
                self._pending_transactions,
                period_start,
                period_end,
                source_note=self.file_label.text(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Couldn't import statement", str(exc))
            return

        self._pending_transactions = []
        self.file_label.setText("No file chosen")
        self.import_btn.setEnabled(False)
        self._notify_change()
        self._select_statement_row(statement.id)

    # -- statements table -------------------------------------------------

    def reload_accounts(self):
        current = self.account_combo.currentData() if self.account_combo.count() else None
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in self.session.query(Account).order_by(Account.name).all():
            self.account_combo.addItem(account.name, account.id)
        idx = self.account_combo.findData(current)
        self.account_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.account_combo.blockSignals(False)

    def refresh(self):
        self._refresh_statements_table()
        self._refresh_suggestions_table()
        self._refresh_detail(None)

    def _refresh_statements_table(self):
        statements = self.session.query(Statement).order_by(Statement.period_start.desc()).all()
        self.statements_table.setRowCount(0)
        for s in statements:
            row = self.statements_table.rowCount()
            self.statements_table.insertRow(row)
            self.statements_table.setItem(row, 0, QTableWidgetItem(s.account.name))
            period = f"{s.period_start.strftime('%d %b %Y')} – {s.period_end.strftime('%d %b %Y')}"
            self.statements_table.setItem(row, 1, QTableWidgetItem(period))
            self.statements_table.setItem(row, 2, QTableWidgetItem(s.imported_at.strftime("%d %b %Y %H:%M")))
            self.statements_table.setItem(row, 3, QTableWidgetItem(str(len(s.transactions))))
            self.statements_table.setItem(row, 4, _money(sum(t.amount for t in s.transactions)))
            self.statements_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, s.id)
        for col in range(4):
            self.statements_table.resizeColumnToContents(col)

    def _select_statement_row(self, statement_id: int):
        for row in range(self.statements_table.rowCount()):
            if self.statements_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == statement_id:
                self.statements_table.selectRow(row)
                return

    def _on_statement_selected(self):
        row = self.statements_table.currentRow()
        if row < 0:
            self._refresh_detail(None)
            return
        statement_id = self.statements_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        statement = self.session.get(Statement, statement_id)
        self._refresh_detail(statement)

    def _delete_statement(self):
        if self._selected_statement is None:
            QMessageBox.information(self, "No selection", "Select a statement to delete first.")
            return
        statement = self._selected_statement
        reply = QMessageBox.question(
            self,
            "Confirm delete",
            f"Delete this statement ({statement.period_start.strftime('%d %b %Y')} – "
            f"{statement.period_end.strftime('%d %b %Y')}) and its {len(statement.transactions)} "
            "transaction(s)? This subtracts its net back out of the account balance.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        account = statement.account
        account.current_balance -= sum(t.amount for t in statement.transactions)
        self.session.delete(statement)
        self.session.commit()
        self._notify_change()

    # -- detail panel: budget vs actual + transactions ---------------------

    def _build_detail_panel(self) -> QWidget:
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)

        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Budget vs Actual</b>"))
        self.report_table = QTableWidget()
        self.report_table.setColumnCount(4)
        self.report_table.setHorizontalHeaderLabels(["Category", "Budgeted", "Actual", "Variance"])
        self.report_table.horizontalHeader().setStretchLastSection(True)
        self.report_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.report_table.verticalHeader().setVisible(False)
        left.addWidget(self.report_table)
        row.addLayout(left, stretch=1)

        right = QVBoxLayout()
        right.addWidget(QLabel("<b>Transactions</b> (double-click Category to reclassify)"))
        self.transactions_table = QTableWidget()
        self.transactions_table.setColumnCount(5)
        self.transactions_table.setHorizontalHeaderLabels(
            ["Date", "Description", "Amount", "Category", "Matched Budget Item"]
        )
        self.transactions_table.horizontalHeader().setStretchLastSection(True)
        self.transactions_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.transactions_table.verticalHeader().setVisible(False)
        self.transactions_table.doubleClicked.connect(self._reclassify_transaction)
        right.addWidget(self.transactions_table)
        row.addLayout(right, stretch=1)

        return panel

    def _add_report_row(
        self,
        category: str,
        budgeted: float | None,
        actual: float | None,
        variance: float | None,
        bold: bool = False,
        bg: QColor | None = None,
    ):
        row = self.report_table.rowCount()
        self.report_table.insertRow(row)
        cat_item = QTableWidgetItem(category)
        self.report_table.setItem(row, 0, cat_item)
        cells = [cat_item]
        for col, value in ((1, budgeted), (2, actual), (3, variance)):
            item = _money(value) if value is not None else QTableWidgetItem("")
            self.report_table.setItem(row, col, item)
            cells.append(item)
        if bold:
            for item in cells:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
        if bg is not None:
            for item in cells:
                item.setBackground(bg)
        elif variance is not None:
            cells[3].setForeground(WARNING if variance < 0 else SUCCESS)

    def _refresh_detail(self, statement: Statement | None):
        self._selected_statement = statement
        self.report_table.setRowCount(0)
        self.transactions_table.setRowCount(0)
        if statement is None:
            return

        report = budget_vs_actual_report(self.session, statement)
        for row in report["by_category"]:
            self._add_report_row(row["category"], row["budgeted"], row["actual"], row["variance"])
        self._add_report_row(
            "Total",
            report["total_budgeted"],
            report["total_actual"],
            report["total_variance"],
            bold=True,
            bg=TOTAL_BG,
        )
        self.report_table.resizeColumnToContents(0)

        for t in sorted(statement.transactions, key=lambda t: t.date):
            row = self.transactions_table.rowCount()
            self.transactions_table.insertRow(row)
            self.transactions_table.setItem(row, 0, QTableWidgetItem(t.date.strftime("%d %b %Y")))
            self.transactions_table.setItem(row, 1, QTableWidgetItem(t.description))
            self.transactions_table.setItem(row, 2, _money(t.amount))
            self.transactions_table.setItem(row, 3, QTableWidgetItem(t.category.name if t.category else "—"))
            matched = t.matched_budget_item.description if t.matched_budget_item else "—"
            self.transactions_table.setItem(row, 4, QTableWidgetItem(matched))
            self.transactions_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, t.id)
        for col in range(4):
            self.transactions_table.resizeColumnToContents(col)

    def _reclassify_transaction(self):
        row = self.transactions_table.currentRow()
        if row < 0:
            return
        transaction_id = self.transactions_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        transaction = self.session.get(Transaction, transaction_id)
        dlg = TransactionCategoryDialog(self.session, transaction, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_detail(self._selected_statement)

    # -- suggestions panel --------------------------------------------------

    def _build_suggestions_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("<b>Budget Suggestions</b>"))

        self.suggestions_table = QTableWidget()
        self.suggestions_table.setColumnCount(7)
        self.suggestions_table.setHorizontalHeaderLabels(
            ["Type", "Description", "Category", "Account", "Current", "Proposed", "Rationale"]
        )
        self.suggestions_table.horizontalHeader().setStretchLastSection(True)
        self.suggestions_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.suggestions_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.suggestions_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.suggestions_table.verticalHeader().setVisible(False)
        self.suggestions_table.setMaximumHeight(180)
        layout.addWidget(self.suggestions_table)

        btn_row = QHBoxLayout()
        accept_btn = QPushButton("Accept")
        accept_btn.setObjectName("primaryButton")
        accept_btn.clicked.connect(self._accept_suggestion)
        reject_btn = QPushButton("Reject")
        reject_btn.clicked.connect(self._reject_suggestion)
        btn_row.addWidget(accept_btn)
        btn_row.addWidget(reject_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        return panel

    def _refresh_suggestions_table(self):
        suggestions = (
            self.session.query(BudgetSuggestion)
            .filter_by(status=SuggestionStatus.PENDING)
            .order_by(BudgetSuggestion.created_at.desc())
            .all()
        )
        self.suggestions_table.setRowCount(0)
        for sg in suggestions:
            row = self.suggestions_table.rowCount()
            self.suggestions_table.insertRow(row)
            self.suggestions_table.setItem(row, 0, QTableWidgetItem(sg.suggestion_type.value))
            self.suggestions_table.setItem(row, 1, QTableWidgetItem(sg.description))
            self.suggestions_table.setItem(row, 2, QTableWidgetItem(sg.category.name))
            self.suggestions_table.setItem(row, 3, QTableWidgetItem(sg.account.name))
            current_text = f"£{sg.current_amount:,.2f}" if sg.current_amount is not None else "—"
            self.suggestions_table.setItem(row, 4, QTableWidgetItem(current_text))
            self.suggestions_table.setItem(row, 5, QTableWidgetItem(f"£{sg.proposed_amount:,.2f}"))
            self.suggestions_table.setItem(row, 6, QTableWidgetItem(sg.rationale or ""))
            self.suggestions_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, sg.id)
        for col in range(6):
            self.suggestions_table.resizeColumnToContents(col)

    def _selected_suggestion(self) -> BudgetSuggestion | None:
        row = self.suggestions_table.currentRow()
        if row < 0:
            return None
        suggestion_id = self.suggestions_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return self.session.get(BudgetSuggestion, suggestion_id)

    def _accept_suggestion(self):
        sg = self._selected_suggestion()
        if sg is None:
            QMessageBox.information(self, "No selection", "Select a suggestion to accept first.")
            return
        accept_suggestion(self.session, sg)
        self._notify_change()

    def _reject_suggestion(self):
        sg = self._selected_suggestion()
        if sg is None:
            QMessageBox.information(self, "No selection", "Select a suggestion to reject first.")
            return
        reject_suggestion(self.session, sg)
        self._notify_change()

    # -- shared ------------------------------------------------------------

    def _notify_change(self):
        self.refresh()
        if self.on_change:
            self.on_change()
