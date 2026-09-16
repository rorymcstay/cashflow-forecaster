import datetime as dt

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
)
from sqlalchemy.orm import Session

from app.forecast import account_run_rate
from app.models import (
    Account,
    BudgetItem,
    Category,
    FlowType,
    Frequency,
    Holding,
    Transaction,
    UpcomingExpense,
    UpcomingExpenseStatus,
)
from app.seed import get_or_create_category
from app.ui import theme


def _to_qdate(d: dt.date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _to_pydate(qd: QDate) -> dt.date:
    return dt.date(qd.year(), qd.month(), qd.day())


def _amount_spinbox(initial: float = 0.0) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setDecimals(2)
    box.setRange(-1_000_000, 1_000_000)
    box.setSingleStep(1.0)
    box.setPrefix("£ ")
    box.setValue(initial)
    return box


class AccountDialog(QDialog):
    def __init__(self, session: Session, obj: Account | None = None, parent=None):
        super().__init__(parent)
        self.session = session
        self.obj = obj
        self.setWindowTitle("Edit Account" if obj else "Add Account")

        self.name_edit = QLineEdit(obj.name if obj else "")
        self.balance_spin = _amount_spinbox(obj.current_balance if obj else 0.0)
        self.as_of_edit = QDateEdit(_to_qdate(obj.balance_as_of if obj else dt.date.today()))
        self.as_of_edit.setCalendarPopup(True)

        self.threshold_check = QCheckBox("Warn when balance drops below")
        self.threshold_spin = _amount_spinbox(
            obj.low_balance_threshold if obj and obj.low_balance_threshold else 0.0
        )
        has_threshold = bool(obj and obj.low_balance_threshold is not None)
        self.threshold_check.setChecked(has_threshold)
        self.threshold_spin.setEnabled(has_threshold)
        self.threshold_check.toggled.connect(self.threshold_spin.setEnabled)

        self.growth_check = QCheckBox("Earns interest / growth")
        self.growth_spin = QDoubleSpinBox()
        self.growth_spin.setDecimals(2)
        self.growth_spin.setRange(0, 100)
        self.growth_spin.setSingleStep(0.1)
        self.growth_spin.setSuffix(" % APY")
        has_growth = bool(obj and obj.growth_rate)
        self.growth_spin.setValue(obj.growth_rate if has_growth else 0.0)
        self.growth_check.setChecked(has_growth)
        self.growth_spin.setEnabled(has_growth)
        self.growth_check.toggled.connect(self.growth_spin.setEnabled)

        self.cc_check = QCheckBox("This is a credit card")
        has_cc = bool(obj and obj.is_credit_card)
        self.cc_check.setChecked(has_cc)

        self.cc_payee_combo = QComboBox()
        other_accounts = sorted(
            (a for a in session.query(Account).all() if not obj or a.id != obj.id), key=lambda a: a.name
        )
        for a in other_accounts:
            self.cc_payee_combo.addItem(a.name, a.id)
        if obj and obj.cc_payee_account_id is not None:
            idx = self.cc_payee_combo.findData(obj.cc_payee_account_id)
            if idx >= 0:
                self.cc_payee_combo.setCurrentIndex(idx)

        self.cc_payment_day_spin = QSpinBox()
        self.cc_payment_day_spin.setRange(1, 31)
        self.cc_payment_day_spin.setValue(obj.cc_payment_day if obj and obj.cc_payment_day else 1)

        self.cc_pay_in_full_check = QCheckBox("Pay balance in full each month")
        has_pay_in_full = bool(obj and obj.cc_pay_in_full)
        self.cc_pay_in_full_check.setChecked(has_pay_in_full)

        self.cc_fixed_payment_spin = _amount_spinbox(
            obj.cc_fixed_payment_amount if obj and obj.cc_fixed_payment_amount else 0.0
        )
        self.cc_fixed_payment_spin.setRange(0, 1_000_000)
        self.cc_fixed_payment_spin.setEnabled(not has_pay_in_full)
        self.cc_pay_in_full_check.toggled.connect(
            lambda checked: self.cc_fixed_payment_spin.setEnabled(not checked)
        )

        self.cc_payee_label = QLabel("Payee Account")
        self.cc_payment_day_label = QLabel("Direct Debit Day")
        self.cc_row_widgets = [
            self.cc_payee_label,
            self.cc_payee_combo,
            self.cc_payment_day_label,
            self.cc_payment_day_spin,
            self.cc_pay_in_full_check,
            self.cc_fixed_payment_spin,
        ]
        for w in self.cc_row_widgets:
            w.setVisible(has_cc)
        self.cc_check.toggled.connect(self._update_cc_visibility)

        if obj is not None:
            self.run_rate_label = QLabel()
            self.run_rate_label.setWordWrap(True)
            self._refresh_run_rate()

            self.transfers_table = QTableWidget(0, 4)
            self.transfers_table.setHorizontalHeaderLabels(
                ["Direction", "Other Account", "Amount", "Schedule"]
            )
            self.transfers_table.horizontalHeader().setStretchLastSection(True)
            self.transfers_table.setMaximumHeight(140)
            self.transfers_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.transfers_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.transfers_table.doubleClicked.connect(self._edit_selected_transfer)
            self._refresh_transfers_table()

            add_recurring_transfer_btn = QPushButton("+ Recurring Transfer")
            add_recurring_transfer_btn.clicked.connect(self._add_recurring_transfer)
            add_oneoff_transfer_btn = QPushButton("+ One-off Transfer")
            add_oneoff_transfer_btn.clicked.connect(self._add_oneoff_transfer)
            remove_transfer_btn = QPushButton("Remove Selected")
            remove_transfer_btn.clicked.connect(self._remove_selected_transfer)
            transfers_buttons = QHBoxLayout()
            transfers_buttons.addWidget(add_recurring_transfer_btn)
            transfers_buttons.addWidget(add_oneoff_transfer_btn)
            transfers_buttons.addWidget(remove_transfer_btn)
            transfers_buttons.addStretch()

        self.holdings_table = QTableWidget(0, 2)
        self.holdings_table.setHorizontalHeaderLabels(["Ticker", "Weight"])
        self.holdings_table.horizontalHeader().setStretchLastSection(True)
        self.holdings_table.setMaximumHeight(120)
        if obj:
            for h in obj.holdings:
                self._add_holding_row(h.ticker, h.weight)

        add_holding_btn = QPushButton("+ Add Ticker")
        add_holding_btn.clicked.connect(lambda: self._add_holding_row("", 1.0))
        remove_holding_btn = QPushButton("Remove Selected")
        remove_holding_btn.clicked.connect(self._remove_selected_holding)
        holdings_buttons = QHBoxLayout()
        holdings_buttons.addWidget(add_holding_btn)
        holdings_buttons.addWidget(remove_holding_btn)
        holdings_buttons.addStretch()

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Current Balance", self.balance_spin)
        form.addRow("Balance As Of", self.as_of_edit)
        form.addRow(self.threshold_check, self.threshold_spin)
        form.addRow(self.growth_check, self.growth_spin)
        form.addRow(self.cc_check)
        form.addRow(self.cc_payee_label, self.cc_payee_combo)
        form.addRow(self.cc_payment_day_label, self.cc_payment_day_spin)
        form.addRow(self.cc_pay_in_full_check, self.cc_fixed_payment_spin)
        if obj is not None:
            form.addRow(self.run_rate_label)
            form.addRow(QLabel("Cross-Account Transfers (recurring + one-off, in and out)"))
            form.addRow(self.transfers_table)
            form.addRow(transfers_buttons)
        form.addRow(QLabel("Investment Holdings (tickers + relative weights; overrides growth above)"))
        form.addRow(self.holdings_table)
        form.addRow(holdings_buttons)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setLayout(form)

    def _add_holding_row(self, ticker: str, weight: float) -> None:
        row = self.holdings_table.rowCount()
        self.holdings_table.insertRow(row)
        self.holdings_table.setItem(row, 0, QTableWidgetItem(ticker))
        weight_item = QTableWidgetItem(f"{weight:g}")
        weight_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.holdings_table.setItem(row, 1, weight_item)

    def _update_cc_visibility(self, checked: bool) -> None:
        for w in self.cc_row_widgets:
            w.setVisible(checked)

    def _refresh_run_rate(self) -> None:
        result = account_run_rate(self.session, self.obj)
        avg = result["avg_monthly_net"]
        overdrawn_date = result["overdrawn_date"]
        lines = []
        if avg < 0:
            lines.append(f"⚠ Net outflow of £{abs(avg):,.2f}/month based on current budget items")
        else:
            lines.append(f"Net inflow of £{avg:,.2f}/month based on current budget items")
        if overdrawn_date is not None:
            lines.append(f"⚠ Projected to go overdrawn around {overdrawn_date.strftime('%d %b %Y')}")
        is_warning = avg < 0 or overdrawn_date is not None
        color = theme.WARNING if is_warning else theme.SUCCESS
        self.run_rate_label.setText("\n".join(lines))
        self.run_rate_label.setStyleSheet(f"color: {color}; font-weight: 600;")

    def _transfer_items(self) -> list[tuple[str, BudgetItem | UpcomingExpense]]:
        """("budget"|"upcoming", obj) for every transfer where this account is
        either the source or the target."""
        budget_items = (
            self.session.query(BudgetItem)
            .filter(
                BudgetItem.flow_type == FlowType.TRANSFER,
                (BudgetItem.account_id == self.obj.id) | (BudgetItem.target_account_id == self.obj.id),
            )
            .all()
        )
        upcoming = (
            self.session.query(UpcomingExpense)
            .filter(
                UpcomingExpense.flow_type == FlowType.TRANSFER,
                (UpcomingExpense.account_id == self.obj.id)
                | (UpcomingExpense.target_account_id == self.obj.id),
            )
            .all()
        )
        items = [("budget", b) for b in budget_items] + [("upcoming", u) for u in upcoming]
        items.sort(key=lambda kv: kv[1].description)
        return items

    def _refresh_transfers_table(self) -> None:
        self.transfers_table.setRowCount(0)
        for kind, item in self._transfer_items():
            outgoing = item.account_id == self.obj.id
            other = item.target_account.name if outgoing else item.account.name
            direction = f"Out to {other}" if outgoing else f"In from {other}"
            schedule = item.frequency.value if kind == "budget" else item.date.strftime("%d %b %Y")

            row = self.transfers_table.rowCount()
            self.transfers_table.insertRow(row)
            direction_item = QTableWidgetItem(direction)
            direction_item.setData(Qt.ItemDataRole.UserRole, (kind, item.id))
            self.transfers_table.setItem(row, 0, direction_item)
            self.transfers_table.setItem(row, 1, QTableWidgetItem(item.description))
            amount_item = QTableWidgetItem(f"£{item.amount:,.2f}")
            amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.transfers_table.setItem(row, 2, amount_item)
            self.transfers_table.setItem(row, 3, QTableWidgetItem(schedule))
        self.transfers_table.resizeColumnsToContents()

    def _selected_transfer(self) -> tuple[str, int] | None:
        row = self.transfers_table.currentRow()
        if row < 0:
            return None
        return self.transfers_table.item(row, 0).data(Qt.ItemDataRole.UserRole)

    def _add_recurring_transfer(self) -> None:
        dlg = BudgetItemDialog(
            self.session, parent=self, initial_account_id=self.obj.id, initial_flow_type=FlowType.TRANSFER
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_transfers_table()
            self._refresh_run_rate()

    def _add_oneoff_transfer(self) -> None:
        dlg = UpcomingExpenseDialog(
            self.session, parent=self, initial_account_id=self.obj.id, initial_flow_type=FlowType.TRANSFER
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_transfers_table()
            self._refresh_run_rate()

    def _edit_selected_transfer(self) -> None:
        selected = self._selected_transfer()
        if selected is None:
            return
        kind, item_id = selected
        if kind == "budget":
            item = self.session.get(BudgetItem, item_id)
            dlg = BudgetItemDialog(self.session, obj=item, parent=self)
        else:
            item = self.session.get(UpcomingExpense, item_id)
            dlg = UpcomingExpenseDialog(self.session, obj=item, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_transfers_table()
            self._refresh_run_rate()

    def _remove_selected_transfer(self) -> None:
        selected = self._selected_transfer()
        if selected is None:
            QMessageBox.information(self, "No selection", "Select a transfer to remove first.")
            return
        kind, item_id = selected
        model = BudgetItem if kind == "budget" else UpcomingExpense
        item = self.session.get(model, item_id)
        if item is not None:
            self.session.delete(item)
            self.session.commit()
        self._refresh_transfers_table()
        self._refresh_run_rate()

    def _remove_selected_holding(self) -> None:
        row = self.holdings_table.currentRow()
        if row >= 0:
            self.holdings_table.removeRow(row)

    def _collect_holdings(self) -> dict[str, float]:
        weights: dict[str, float] = {}
        for row in range(self.holdings_table.rowCount()):
            ticker_item = self.holdings_table.item(row, 0)
            weight_item = self.holdings_table.item(row, 1)
            ticker = ticker_item.text().strip().upper() if ticker_item else ""
            if not ticker:
                continue
            try:
                weight = float(weight_item.text()) if weight_item else 0.0
            except ValueError:
                weight = 0.0
            if weight > 0:
                weights[ticker] = weights.get(ticker, 0.0) + weight
        return weights

    def on_accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Please enter an account name.")
            return
        existing = self.session.query(Account).filter(Account.name == name).one_or_none()
        if existing is not None and existing is not self.obj:
            QMessageBox.warning(self, "Duplicate name", "An account with this name already exists.")
            return

        threshold = self.threshold_spin.value() if self.threshold_check.isChecked() else None
        growth_rate = self.growth_spin.value() if self.growth_check.isChecked() else None
        holdings = self._collect_holdings()

        is_credit_card = self.cc_check.isChecked()
        if is_credit_card and self.cc_payee_combo.count() == 0:
            QMessageBox.warning(self, "No payee account", "Add another account first to pay this card from.")
            return
        cc_payee_account_id = self.cc_payee_combo.currentData() if is_credit_card else None
        cc_payment_day = self.cc_payment_day_spin.value() if is_credit_card else None
        cc_pay_in_full = self.cc_pay_in_full_check.isChecked() if is_credit_card else False
        cc_fixed_payment_amount = (
            self.cc_fixed_payment_spin.value() if is_credit_card and not cc_pay_in_full else None
        )

        if self.obj is None:
            self.obj = Account(
                name=name,
                current_balance=self.balance_spin.value(),
                balance_as_of=_to_pydate(self.as_of_edit.date()),
                low_balance_threshold=threshold,
                growth_rate=growth_rate,
                is_credit_card=is_credit_card,
                cc_payee_account_id=cc_payee_account_id,
                cc_payment_day=cc_payment_day,
                cc_pay_in_full=cc_pay_in_full,
                cc_fixed_payment_amount=cc_fixed_payment_amount,
            )
            self.session.add(self.obj)
        else:
            self.obj.name = name
            self.obj.current_balance = self.balance_spin.value()
            self.obj.balance_as_of = _to_pydate(self.as_of_edit.date())
            self.obj.low_balance_threshold = threshold
            self.obj.growth_rate = growth_rate
            self.obj.is_credit_card = is_credit_card
            self.obj.cc_payee_account_id = cc_payee_account_id
            self.obj.cc_payment_day = cc_payment_day
            self.obj.cc_pay_in_full = cc_pay_in_full
            self.obj.cc_fixed_payment_amount = cc_fixed_payment_amount
            for h in list(self.obj.holdings):
                self.session.delete(h)

        for ticker, weight in holdings.items():
            self.session.add(Holding(account=self.obj, ticker=ticker, weight=weight))

        self.session.commit()
        self.accept()


class BudgetItemDialog(QDialog):
    def __init__(
        self,
        session: Session,
        obj: BudgetItem | None = None,
        parent=None,
        initial_account_id: int | None = None,
        initial_flow_type: FlowType | None = None,
        initial_description: str | None = None,
        initial_amount: float | None = None,
        initial_category_name: str | None = None,
    ):
        super().__init__(parent)
        self.session = session
        self.obj = obj
        self.setWindowTitle("Edit Budget Item" if obj else "Add Budget Item")

        self.desc_edit = QLineEdit(obj.description if obj else (initial_description or ""))
        self.amount_spin = _amount_spinbox(
            obj.amount if obj else (initial_amount if initial_amount is not None else 0.0)
        )
        self.amount_spin.setRange(0, 1_000_000)

        self.flow_combo = QComboBox()
        for ft in FlowType:
            self.flow_combo.addItem(ft.value, ft)
        default_flow_type = initial_flow_type if obj is None and initial_flow_type else FlowType.EXPENSE
        self.flow_combo.setCurrentIndex(self.flow_combo.findData(obj.flow_type if obj else default_flow_type))

        self.freq_combo = QComboBox()
        for f in Frequency:
            self.freq_combo.addItem(f.value, f)
        if obj:
            self.freq_combo.setCurrentIndex(self.freq_combo.findData(obj.frequency))
        else:
            self.freq_combo.setCurrentIndex(self.freq_combo.findData(Frequency.MONTHLY))

        self.from_edit = QDateEdit(_to_qdate(obj.effective_from if obj else dt.date.today()))
        self.from_edit.setCalendarPopup(True)

        self.has_until_check = QCheckBox("Has an end date")
        self.until_edit = QDateEdit(
            _to_qdate(obj.effective_until if obj and obj.effective_until else dt.date.today())
        )
        self.until_edit.setCalendarPopup(True)
        has_until = bool(obj and obj.effective_until is not None)
        self.has_until_check.setChecked(has_until)
        self.until_edit.setEnabled(has_until)
        self.has_until_check.toggled.connect(self.until_edit.setEnabled)

        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        categories = sorted(session.query(Category).all(), key=lambda c: c.name)
        for c in categories:
            self.category_combo.addItem(c.name)
        if obj:
            self.category_combo.setCurrentText(obj.category.name)
        elif initial_category_name:
            self.category_combo.setCurrentText(initial_category_name)
        elif default_flow_type == FlowType.TRANSFER:
            self.category_combo.setCurrentText("Transfers")

        self.account_combo = QComboBox()
        accounts = sorted(session.query(Account).all(), key=lambda a: a.name)
        for a in accounts:
            self.account_combo.addItem(a.name, a.id)
        if obj:
            self.account_combo.setCurrentIndex(self.account_combo.findData(obj.account_id))
        elif initial_account_id is not None:
            idx = self.account_combo.findData(initial_account_id)
            if idx >= 0:
                self.account_combo.setCurrentIndex(idx)

        self.target_account_combo = QComboBox()
        for a in accounts:
            self.target_account_combo.addItem(a.name, a.id)
        if obj and obj.target_account_id is not None:
            self.target_account_combo.setCurrentIndex(
                self.target_account_combo.findData(obj.target_account_id)
            )
        self.target_account_row_label = QLabel("Transfer To")
        self._update_target_account_visibility(self.flow_combo.currentData())
        self.flow_combo.currentIndexChanged.connect(
            lambda: self._update_target_account_visibility(self.flow_combo.currentData())
        )

        self.notes_edit = QLineEdit(obj.notes or "" if obj else "")

        form = QFormLayout()
        form.addRow("Description", self.desc_edit)
        form.addRow("Amount", self.amount_spin)
        form.addRow("Type", self.flow_combo)
        form.addRow("Frequency", self.freq_combo)
        form.addRow("Effective From", self.from_edit)
        form.addRow(self.has_until_check, self.until_edit)
        form.addRow("Category", self.category_combo)
        form.addRow("Account", self.account_combo)
        form.addRow(self.target_account_row_label, self.target_account_combo)
        form.addRow("Notes", self.notes_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setLayout(form)

    def _update_target_account_visibility(self, flow_type: FlowType) -> None:
        is_transfer = flow_type == FlowType.TRANSFER
        self.target_account_row_label.setVisible(is_transfer)
        self.target_account_combo.setVisible(is_transfer)

    def on_accept(self):
        description = self.desc_edit.text().strip()
        if not description:
            QMessageBox.warning(self, "Missing description", "Please enter a description.")
            return
        if self.account_combo.count() == 0:
            QMessageBox.warning(self, "No accounts", "Add an account first, on the Accounts tab.")
            return

        category_name = self.category_combo.currentText().strip()
        if not category_name:
            QMessageBox.warning(self, "Missing category", "Please enter or choose a category.")
            return

        effective_from = _to_pydate(self.from_edit.date())
        effective_until = _to_pydate(self.until_edit.date()) if self.has_until_check.isChecked() else None
        if effective_until is not None and effective_until < effective_from:
            QMessageBox.warning(self, "Invalid dates", "Effective Until must be on or after Effective From.")
            return

        flow_type = self.flow_combo.currentData()
        target_account_id = (
            self.target_account_combo.currentData() if flow_type == FlowType.TRANSFER else None
        )
        if flow_type == FlowType.TRANSFER and target_account_id is None:
            QMessageBox.warning(self, "Missing target account", "Choose an account to transfer to.")
            return

        category = get_or_create_category(self.session, category_name)
        account_id = self.account_combo.currentData()

        if self.obj is None:
            self.obj = BudgetItem(
                description=description,
                amount=self.amount_spin.value(),
                flow_type=flow_type,
                frequency=self.freq_combo.currentData(),
                effective_from=effective_from,
                effective_until=effective_until,
                notes=self.notes_edit.text().strip() or None,
                category=category,
                account_id=account_id,
                target_account_id=target_account_id,
            )
            self.session.add(self.obj)
        else:
            self.obj.description = description
            self.obj.amount = self.amount_spin.value()
            self.obj.flow_type = flow_type
            self.obj.frequency = self.freq_combo.currentData()
            self.obj.effective_from = effective_from
            self.obj.effective_until = effective_until
            self.obj.notes = self.notes_edit.text().strip() or None
            self.obj.category = category
            self.obj.account_id = account_id
            self.obj.target_account_id = target_account_id

        self.session.commit()
        self.accept()


class TransactionCategoryDialog(QDialog):
    def __init__(self, session: Session, transaction: Transaction, parent=None):
        super().__init__(parent)
        self.session = session
        self.transaction = transaction
        self.setWindowTitle("Reclassify Transaction")

        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        categories = sorted(session.query(Category).all(), key=lambda c: c.name)
        for c in categories:
            self.category_combo.addItem(c.name)
        if transaction.category:
            self.category_combo.setCurrentText(transaction.category.name)

        form = QFormLayout()
        form.addRow("Description", QLabel(transaction.description))
        form.addRow("Amount", QLabel(f"£{transaction.amount:,.2f}"))
        form.addRow("Category", self.category_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setLayout(form)

    def on_accept(self):
        category_name = self.category_combo.currentText().strip()
        if not category_name:
            QMessageBox.warning(self, "Missing category", "Please enter or choose a category.")
            return
        self.transaction.category = get_or_create_category(self.session, category_name)
        self.session.commit()
        self.accept()


class UpcomingExpenseDialog(QDialog):
    def __init__(
        self,
        session: Session,
        obj: UpcomingExpense | None = None,
        parent=None,
        initial_account_id: int | None = None,
        initial_flow_type: FlowType | None = None,
    ):
        super().__init__(parent)
        self.session = session
        self.obj = obj
        self.setWindowTitle("Edit Upcoming Expense" if obj else "Add Upcoming Expense")

        self.date_edit = QDateEdit(_to_qdate(obj.date if obj else dt.date.today()))
        self.date_edit.setCalendarPopup(True)

        self.desc_edit = QLineEdit(obj.description if obj else "")

        self.amount_spin = _amount_spinbox(obj.amount if obj else 0.0)
        self.amount_spin.setRange(0, 1_000_000)

        self.flow_combo = QComboBox()
        for ft in FlowType:
            self.flow_combo.addItem(ft.value, ft)
        default_flow_type = initial_flow_type if obj is None and initial_flow_type else FlowType.EXPENSE
        self.flow_combo.setCurrentIndex(self.flow_combo.findData(obj.flow_type if obj else default_flow_type))

        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        categories = sorted(session.query(Category).all(), key=lambda c: c.name)
        for c in categories:
            self.category_combo.addItem(c.name)
        if obj:
            self.category_combo.setCurrentText(obj.category.name)
        elif default_flow_type == FlowType.TRANSFER:
            self.category_combo.setCurrentText("Transfers")

        self.account_combo = QComboBox()
        accounts = sorted(session.query(Account).all(), key=lambda a: a.name)
        for a in accounts:
            self.account_combo.addItem(a.name, a.id)
        if obj:
            self.account_combo.setCurrentIndex(self.account_combo.findData(obj.account_id))
        elif initial_account_id is not None:
            idx = self.account_combo.findData(initial_account_id)
            if idx >= 0:
                self.account_combo.setCurrentIndex(idx)

        self.target_account_combo = QComboBox()
        for a in accounts:
            self.target_account_combo.addItem(a.name, a.id)
        if obj and obj.target_account_id is not None:
            self.target_account_combo.setCurrentIndex(
                self.target_account_combo.findData(obj.target_account_id)
            )
        self.target_account_row_label = QLabel("Transfer To")
        self._update_target_account_visibility(self.flow_combo.currentData())
        self.flow_combo.currentIndexChanged.connect(
            lambda: self._update_target_account_visibility(self.flow_combo.currentData())
        )

        form = QFormLayout()
        form.addRow("Date", self.date_edit)
        form.addRow("Description", self.desc_edit)
        form.addRow("Amount", self.amount_spin)
        form.addRow("Type", self.flow_combo)
        form.addRow("Category", self.category_combo)
        form.addRow("Account", self.account_combo)
        form.addRow(self.target_account_row_label, self.target_account_combo)
        if obj:
            status_label = QLabel(obj.status.value)
            if obj.status == UpcomingExpenseStatus.NEEDS_REVIEW:
                status_label.setText(f"{obj.status.value} — changing the date below will reschedule it")
            form.addRow("Status", status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setLayout(form)

    def _update_target_account_visibility(self, flow_type: FlowType) -> None:
        is_transfer = flow_type == FlowType.TRANSFER
        self.target_account_row_label.setVisible(is_transfer)
        self.target_account_combo.setVisible(is_transfer)

    def on_accept(self):
        description = self.desc_edit.text().strip()
        if not description:
            QMessageBox.warning(self, "Missing description", "Please enter a description.")
            return
        if self.account_combo.count() == 0:
            QMessageBox.warning(self, "No accounts", "Add an account first, on the Accounts tab.")
            return
        category_name = self.category_combo.currentText().strip()
        if not category_name:
            QMessageBox.warning(self, "Missing category", "Please enter or choose a category.")
            return

        flow_type = self.flow_combo.currentData()
        target_account_id = (
            self.target_account_combo.currentData() if flow_type == FlowType.TRANSFER else None
        )
        if flow_type == FlowType.TRANSFER and target_account_id is None:
            QMessageBox.warning(self, "Missing target account", "Choose an account to transfer to.")
            return

        category = get_or_create_category(self.session, category_name)
        account_id = self.account_combo.currentData()

        if self.obj is None:
            self.obj = UpcomingExpense(
                date=_to_pydate(self.date_edit.date()),
                description=description,
                amount=self.amount_spin.value(),
                flow_type=flow_type,
                category=category,
                account_id=account_id,
                target_account_id=target_account_id,
            )
            self.session.add(self.obj)
        else:
            new_date = _to_pydate(self.date_edit.date())
            if new_date != self.obj.date and self.obj.status == UpcomingExpenseStatus.NEEDS_REVIEW:
                self.obj.status = UpcomingExpenseStatus.PENDING
                self.obj.matched_transaction = None
                self.obj.last_seen_statement = None
            self.obj.date = new_date
            self.obj.description = description
            self.obj.amount = self.amount_spin.value()
            self.obj.flow_type = flow_type
            self.obj.category = category
            self.obj.account_id = account_id
            self.obj.target_account_id = target_account_id

        self.session.commit()
        self.accept()
