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
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.models import Account, BudgetSuggestion, Statement, SuggestionStatus, Transaction
from app.pdf_statement_parsers import parse_pdf_statement
from app.statement_import import (
    accept_suggestion,
    budget_vs_actual_report,
    detect_account_for_hint,
    find_statement_gaps,
    import_statement,
    reject_suggestion,
)
from app.statements import extract_csv_transactions
from app.ui import theme
from app.ui.dialogs import TransactionCategoryDialog
from app.ui.widgets import AccountMultiSelect, CollapsibleSection

SECTION_BG = QColor(theme.SECTION_BG)
TOTAL_BG = QColor(theme.TOTAL_BG)
SUCCESS = QColor(theme.SUCCESS)
WARNING = QColor(theme.WARNING)

_KIND_LABELS = {"hsbc_premier": "HSBC Premier PDF", "amex": "Amex PDF", "csv": "CSV"}


def _to_pydate(qd: QDate) -> dt.date:
    return dt.date(qd.year(), qd.month(), qd.day())


def _to_qdate(d: dt.date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _money(value: float) -> QTableWidgetItem:
    item = QTableWidgetItem(f"£{value:,.2f}")
    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


class StatementsScreen(QWidget):
    """Import bank/card statements (CSV or PDF, by browsing or dragging files
    onto this screen), review the classified transactions and budget-vs-actual
    report for that billing period, accept/reject budget suggestions, and see
    which months have no statement coverage yet.

    PDFs are only structurally parsed for the HSBC Premier and Amex layouts
    (see app/pdf_statement_parsers.py) — anything else still goes through
    chat: read_pdf_statement -> transcribe -> the import_statement MCP tool.
    """

    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.on_change = on_change
        self._selected_statement: Statement | None = None
        self._pending_entries: list[dict] = []
        self.setAcceptDrops(True)

        outer_layout = QVBoxLayout(self)
        outer_layout.addWidget(QLabel("<h2>Statements</h2>"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer_layout.addWidget(scroll, stretch=1)

        self.import_section = CollapsibleSection("Import Statements", expanded=True)
        self.import_section.content_layout.addWidget(self._build_import_panel())
        layout.addWidget(self.import_section)

        self.pending_section = CollapsibleSection("Pending Imports", expanded=True)
        pending_header_row = QHBoxLayout()
        self.pending_placeholder = QLabel("Drop a file above to see it here before importing.")
        self.pending_placeholder.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        pending_header_row.addWidget(self.pending_placeholder, stretch=1)
        self.import_all_btn = QPushButton("Import All")
        self.import_all_btn.setObjectName("primaryButton")
        self.import_all_btn.setEnabled(False)
        self.import_all_btn.clicked.connect(self._import_all)
        pending_header_row.addWidget(self.import_all_btn)
        self.pending_section.content_layout.addLayout(pending_header_row)

        # Dropping several files at once can produce more cards than fit on
        # screen — without its own scroll area, the ones below the fold were
        # simply unreachable (no Import/Discard button to click).
        pending_scroll = QScrollArea()
        pending_scroll.setWidgetResizable(True)
        pending_scroll.setMaximumHeight(360)
        pending_scroll.setFrameShape(QFrame.Shape.NoFrame)
        pending_inner = QWidget()
        self.pending_layout = QVBoxLayout(pending_inner)
        self.pending_layout.addStretch()
        pending_scroll.setWidget(pending_inner)
        self.pending_section.content_layout.addWidget(pending_scroll)
        layout.addWidget(self.pending_section)

        self.imported_section = CollapsibleSection("Imported Statements", expanded=True)
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
        self.imported_section.content_layout.addWidget(self.statements_table)

        delete_row = QHBoxLayout()
        delete_row.addStretch()
        delete_statement_btn = QPushButton("Delete Statement")
        delete_statement_btn.clicked.connect(self._delete_statement)
        delete_row.addWidget(delete_statement_btn)
        self.imported_section.content_layout.addLayout(delete_row)
        layout.addWidget(self.imported_section)

        self.detail_section = CollapsibleSection("Budget vs Actual / Transactions", expanded=False)
        self.detail_section.content_layout.addWidget(self._build_detail_panel())
        layout.addWidget(self.detail_section)

        self.suggestions_section = CollapsibleSection("Budget Suggestions", expanded=False)
        self.suggestions_section.content_layout.addWidget(self._build_suggestions_panel())
        layout.addWidget(self.suggestions_section)

        self.gaps_section = CollapsibleSection("Missing Statements", expanded=False)
        self.gaps_section.content_layout.addWidget(self._build_gaps_panel())
        layout.addWidget(self.gaps_section)

        layout.addStretch()

        self.reload_accounts()
        self.refresh()

    # -- drag & drop ---------------------------------------------------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        for path in paths:
            self._process_file(path)
        event.acceptProposedAction()

    # -- import panel --------------------------------------------------

    def _build_import_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(f"QFrame {{ border: 2px dashed {theme.BORDER}; border-radius: 8px; }}")
        outer = QVBoxLayout(panel)

        hint = QLabel("Drag & drop CSV or PDF statements here")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        outer.addWidget(hint)

        browse_row = QHBoxLayout()
        browse_row.addStretch()
        browse_btn = QPushButton("Browse Files…")
        browse_btn.clicked.connect(self._choose_files)
        browse_row.addWidget(browse_btn)
        browse_row.addStretch()
        outer.addLayout(browse_row)

        note = QLabel(
            "PDFs are structurally parsed for HSBC Premier and Amex layouts only — anything else falls "
            "back to reading it via chat (read_pdf_statement) and importing with the import_statement "
            "tool instead."
        )
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        outer.addWidget(note)
        return panel

    def _choose_files(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose Statement Files", "", "Statements (*.csv *.pdf)"
        )
        for path in file_paths:
            self._process_file(path)

    def _process_file(self, path: str):
        suffix = Path(path).suffix.lower()
        try:
            if suffix == ".csv":
                parsed = {
                    "kind": "csv",
                    "transactions": extract_csv_transactions(path),
                    "period_start": None,
                    "period_end": None,
                    "account_hint": None,
                    "reconciliation": None,
                }
            elif suffix == ".pdf":
                parsed = parse_pdf_statement(path)
            else:
                QMessageBox.warning(self, "Unsupported file", f"Unsupported file type: {Path(path).name}")
                return
        except Exception as exc:
            QMessageBox.warning(self, "Couldn't read file", f"{Path(path).name}: {exc}")
            return

        if not parsed["transactions"]:
            if suffix == ".pdf" and parsed.get("kind") is None:
                QMessageBox.information(
                    self,
                    "Unrecognised PDF",
                    f"Couldn't recognise the PDF format of {Path(path).name} — read it via chat "
                    "(read_pdf_statement) and import with the import_statement tool instead.",
                )
            else:
                QMessageBox.warning(
                    self, "No transactions found", f"{Path(path).name} didn't contain any transactions."
                )
            return

        self._add_pending_card(Path(path).name, parsed)

    # -- pending imports -------------------------------------------------

    def _pending_count(self) -> int:
        return self.pending_layout.count() - 1  # exclude the trailing stretch

    def _refresh_pending_title(self) -> None:
        count = self._pending_count()
        self.pending_section.set_title(f"Pending Imports ({count})" if count else "Pending Imports")
        self.import_all_btn.setText(f"Import All ({count})" if count else "Import All")
        self.import_all_btn.setEnabled(count > 0)

    def _run_pending_import(self, entry: dict) -> tuple[bool, str, str]:
        """Import one pending entry. Returns (ok, dialog_title, message) —
        used by both the per-card Import button and Import All, so both
        report the exact same success/historical/failure wording."""
        account_id = entry["account_combo"].currentData()
        if account_id is None:
            return False, "No account", "Choose an account first."
        account = self.session.get(Account, account_id)
        if account is None:
            return False, "No account", "Choose an account first."
        period_start = _to_pydate(entry["start_edit"].date())
        period_end = _to_pydate(entry["end_edit"].date())
        is_reimport = (
            self.session.query(Statement)
            .filter_by(account_id=account.id, period_start=period_start, period_end=period_end)
            .first()
            is not None
        )
        balance_before = account.current_balance
        try:
            import_statement(
                self.session,
                account,
                entry["parsed"]["transactions"],
                period_start,
                period_end,
                source_note=entry["filename"],
                closing_balance=entry["parsed"].get("closing_balance"),
            )
        except ValueError as exc:
            return False, "Couldn't import statement", str(exc)
        verb = "Updated" if is_reimport else "Imported"
        if account.current_balance == balance_before:
            historical_msg = (
                f"{verb} {entry['filename']}. Balance was already known as of "
                f"{account.balance_as_of.strftime('%d %b %Y')}, so it wasn't changed."
            )
            return True, f"{verb} as historical", historical_msg
        return True, verb, f"{verb} {entry['filename']}. Balance now £{account.current_balance:,.2f}."

    def _import_all(self):
        # Chronological order matters: the same-account balance math
        # (front/historical/forward-extends) depends on each import seeing
        # the account's balance_as_of as it would after every earlier-dated
        # statement in the batch has already landed.
        entries = sorted(self._pending_entries, key=lambda e: _to_pydate(e["start_edit"].date()))
        if not entries:
            return
        results = []
        for entry in entries:
            ok, _title, msg = self._run_pending_import(entry)
            results.append((entry["filename"], ok, msg))
            if ok:
                entry["discard"]()
        self._notify_change()
        successes = sum(1 for _, ok, _ in results if ok)
        lines = [f"Imported {successes} of {len(results)} statement(s)."]
        failures = [(fn, msg) for fn, ok, msg in results if not ok]
        if failures:
            lines.append("")
            lines.append("Not imported:")
            lines.extend(f"• {fn}: {msg}" for fn, msg in failures)
        QMessageBox.information(self, "Import All", "\n".join(lines))

    def _add_pending_card(self, filename: str, parsed: dict):
        self.pending_placeholder.setVisible(False)
        self.pending_section.set_expanded(True)
        accounts = self.session.query(Account).order_by(Account.name).all()
        hint = parsed.get("account_hint")
        default_account_id = detect_account_for_hint(accounts, hint)

        dates = [dt.date.fromisoformat(t["date"]) for t in parsed["transactions"]]
        period_start = (
            dt.date.fromisoformat(parsed["period_start"]) if parsed.get("period_start") else min(dates)
        )
        period_end = dt.date.fromisoformat(parsed["period_end"]) if parsed.get("period_end") else max(dates)

        card = QFrame()
        card.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        outer = QVBoxLayout(card)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel(f"<b>{filename}</b>"))
        kind_label = QLabel(_KIND_LABELS.get(parsed.get("kind"), "PDF"))
        kind_label.setStyleSheet(
            f"background-color: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; "
            "padding: 1px 8px; border-radius: 4px;"
        )
        top_row.addWidget(kind_label)
        if hint:
            if default_account_id is not None:
                account_name = next(a.name for a in accounts if a.id == default_account_id)
                detected_label = QLabel(f"Detected account: {account_name}")
                detected_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            else:
                detected_label = QLabel(f"Detected bank: {hint} — choose the account below")
                detected_label.setStyleSheet(f"color: {theme.WARNING};")
            top_row.addWidget(detected_label)
        top_row.addStretch()
        outer.addLayout(top_row)

        mid_row = QHBoxLayout()
        mid_row.addWidget(QLabel("Account:"))
        account_combo = QComboBox()
        for a in accounts:
            account_combo.addItem(a.name, a.id)
        if default_account_id is not None:
            idx = account_combo.findData(default_account_id)
            if idx >= 0:
                account_combo.setCurrentIndex(idx)
        mid_row.addWidget(account_combo)
        mid_row.addWidget(QLabel("Period:"))
        start_edit = QDateEdit(_to_qdate(period_start))
        start_edit.setCalendarPopup(True)
        mid_row.addWidget(start_edit)
        mid_row.addWidget(QLabel("to"))
        end_edit = QDateEdit(_to_qdate(period_end))
        end_edit.setCalendarPopup(True)
        mid_row.addWidget(end_edit)
        count_label = QLabel(f"{len(parsed['transactions'])} transactions")
        count_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        mid_row.addWidget(count_label)
        mid_row.addStretch()
        outer.addLayout(mid_row)

        recon = parsed.get("reconciliation")
        if recon is not None:
            if recon["ok"]:
                recon_label = QLabel(
                    f"✓ Reconciles with the statement's own summary — in £{recon['actual_in']:,.2f}, "
                    f"out £{recon['actual_out']:,.2f}"
                )
                recon_label.setStyleSheet(f"color: {theme.SUCCESS}; font-size: 11px;")
            else:
                recon_label = QLabel(
                    "⚠ Doesn't reconcile with the statement's own summary — expected in "
                    f"£{recon['expected_in']:,.2f} / out £{recon['expected_out']:,.2f}, parsed in "
                    f"£{recon['actual_in']:,.2f} / out £{recon['actual_out']:,.2f}. Review before importing."
                )
                recon_label.setStyleSheet(f"color: {theme.WARNING}; font-size: 11px;")
            recon_label.setWordWrap(True)
            outer.addWidget(recon_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        discard_btn = QPushButton("Discard")
        import_btn = QPushButton("Import")
        import_btn.setObjectName("primaryButton")
        btn_row.addWidget(discard_btn)
        btn_row.addWidget(import_btn)
        outer.addLayout(btn_row)

        # Insert before the trailing stretch (always the last item) so new
        # cards stack from the top instead of ending up after it.
        self.pending_layout.insertWidget(self.pending_layout.count() - 1, card)
        self._refresh_pending_title()

        entry = {
            "filename": filename,
            "parsed": parsed,
            "account_combo": account_combo,
            "start_edit": start_edit,
            "end_edit": end_edit,
            "card": card,
        }

        def discard():
            if entry in self._pending_entries:
                self._pending_entries.remove(entry)
            self.pending_layout.removeWidget(card)
            # hide() immediately — removeWidget() alone leaves the widget
            # visible at its old position (just unmanaged) until deleteLater()'s
            # deferred deletion actually runs on the next event loop turn.
            card.hide()
            card.deleteLater()
            if self.pending_layout.count() == 1:  # nothing left but the trailing stretch
                self.pending_placeholder.setVisible(True)
            self._refresh_pending_title()

        entry["discard"] = discard
        self._pending_entries.append(entry)
        self._refresh_pending_title()

        def do_import():
            ok, title, msg = self._run_pending_import(entry)
            if not ok:
                QMessageBox.warning(self, title, msg)
                return
            discard()
            self._notify_change()
            QMessageBox.information(self, title, msg)

        discard_btn.clicked.connect(discard)
        import_btn.clicked.connect(do_import)

    # -- statements table -------------------------------------------------

    def reload_accounts(self):
        accounts = self.session.query(Account).order_by(Account.name).all()
        history_ids = {row[0] for row in self.session.query(Statement.account_id).distinct().all()}
        self.gap_account_select.set_accounts(accounts, default_checked_ids=history_ids)

    def refresh(self):
        self._refresh_statements_table()
        self._refresh_suggestions_table()
        self._refresh_detail(None)
        self._refresh_gaps()

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
        self.imported_section.set_title(f"Imported Statements ({len(statements)})")

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
        self.detail_section.set_expanded(True)

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
        self.suggestions_section.set_title(f"Budget Suggestions ({len(suggestions)})")

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

    # -- missing statements panel --------------------------------------------

    def _build_gaps_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Accounts:"))
        self.gap_account_select = AccountMultiSelect(self)
        self.gap_account_select.selectionChanged.connect(self._refresh_gaps)
        row.addWidget(self.gap_account_select)
        row.addWidget(QLabel("From:"))
        self.gap_start_edit = QDateEdit(_to_qdate(dt.date.today() - dt.timedelta(days=365)))
        self.gap_start_edit.setCalendarPopup(True)
        row.addWidget(self.gap_start_edit)
        row.addWidget(QLabel("to"))
        self.gap_end_edit = QDateEdit(QDate.currentDate())
        self.gap_end_edit.setCalendarPopup(True)
        row.addWidget(self.gap_end_edit)
        check_btn = QPushButton("Check")
        check_btn.clicked.connect(self._refresh_gaps)
        row.addWidget(check_btn)
        row.addStretch()
        layout.addLayout(row)

        self.gaps_container = QVBoxLayout()
        layout.addLayout(self.gaps_container)
        return panel

    def _refresh_gaps(self):
        while self.gaps_container.count():
            item = self.gaps_container.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        start = _to_pydate(self.gap_start_edit.date())
        end = _to_pydate(self.gap_end_edit.date())
        found_any = False
        for account_id in self.gap_account_select.checked_ids():
            account = self.session.get(Account, account_id)
            if account is None:
                continue
            gaps = find_statement_gaps(self.session, account_id, start, end)
            if not gaps:
                continue
            found_any = True
            gap_row = QHBoxLayout()
            name_label = QLabel(account.name)
            name_label.setStyleSheet("font-weight: bold;")
            name_label.setFixedWidth(160)
            gap_row.addWidget(name_label)
            gaps_label = QLabel(", ".join(g["label"] for g in gaps))
            gaps_label.setWordWrap(True)
            gaps_label.setStyleSheet(f"color: {theme.WARNING};")
            gap_row.addWidget(gaps_label, stretch=1)
            self.gaps_container.addLayout(gap_row)
        if not found_any:
            label = QLabel("No gaps — every selected account has statement coverage across this range.")
            label.setStyleSheet(f"color: {theme.SUCCESS};")
            self.gaps_container.addWidget(label)
        else:
            self.gaps_section.set_expanded(True)
        self.gaps_section.set_title("Missing Statements ⚠" if found_any else "Missing Statements")

    # -- shared ------------------------------------------------------------

    def _notify_change(self):
        self.refresh()
        if self.on_change:
            self.on_change()
