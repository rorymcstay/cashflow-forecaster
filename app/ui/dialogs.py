import datetime as dt

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
)
from sqlalchemy.orm import Session

from app.models import Account, BudgetItem, Category, FlowType, Frequency, Transaction, UpcomingExpense
from app.seed import get_or_create_category


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

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Current Balance", self.balance_spin)
        form.addRow("Balance As Of", self.as_of_edit)
        form.addRow(self.threshold_check, self.threshold_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setLayout(form)

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

        if self.obj is None:
            self.obj = Account(
                name=name,
                current_balance=self.balance_spin.value(),
                balance_as_of=_to_pydate(self.as_of_edit.date()),
                low_balance_threshold=threshold,
            )
            self.session.add(self.obj)
        else:
            self.obj.name = name
            self.obj.current_balance = self.balance_spin.value()
            self.obj.balance_as_of = _to_pydate(self.as_of_edit.date())
            self.obj.low_balance_threshold = threshold

        self.session.commit()
        self.accept()


class BudgetItemDialog(QDialog):
    def __init__(self, session: Session, obj: BudgetItem | None = None, parent=None):
        super().__init__(parent)
        self.session = session
        self.obj = obj
        self.setWindowTitle("Edit Budget Item" if obj else "Add Budget Item")

        self.desc_edit = QLineEdit(obj.description if obj else "")
        self.amount_spin = _amount_spinbox(obj.amount if obj else 0.0)
        self.amount_spin.setRange(0, 1_000_000)

        self.flow_combo = QComboBox()
        for ft in FlowType:
            self.flow_combo.addItem(ft.value, ft)
        self.flow_combo.setCurrentIndex(self.flow_combo.findData(obj.flow_type if obj else FlowType.EXPENSE))

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

        self.account_combo = QComboBox()
        accounts = sorted(session.query(Account).all(), key=lambda a: a.name)
        for a in accounts:
            self.account_combo.addItem(a.name, a.id)
        if obj:
            self.account_combo.setCurrentIndex(self.account_combo.findData(obj.account_id))

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
        form.addRow("Notes", self.notes_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setLayout(form)

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

        category = get_or_create_category(self.session, category_name)
        account_id = self.account_combo.currentData()

        if self.obj is None:
            self.obj = BudgetItem(
                description=description,
                amount=self.amount_spin.value(),
                flow_type=self.flow_combo.currentData(),
                frequency=self.freq_combo.currentData(),
                effective_from=effective_from,
                effective_until=effective_until,
                notes=self.notes_edit.text().strip() or None,
                category=category,
                account_id=account_id,
            )
            self.session.add(self.obj)
        else:
            self.obj.description = description
            self.obj.amount = self.amount_spin.value()
            self.obj.flow_type = self.flow_combo.currentData()
            self.obj.frequency = self.freq_combo.currentData()
            self.obj.effective_from = effective_from
            self.obj.effective_until = effective_until
            self.obj.notes = self.notes_edit.text().strip() or None
            self.obj.category = category
            self.obj.account_id = account_id

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
    def __init__(self, session: Session, obj: UpcomingExpense | None = None, parent=None):
        super().__init__(parent)
        self.session = session
        self.obj = obj
        self.setWindowTitle("Edit Upcoming Expense" if obj else "Add Upcoming Expense")

        self.date_edit = QDateEdit(_to_qdate(obj.date if obj else dt.date.today()))
        self.date_edit.setCalendarPopup(True)

        self.desc_edit = QLineEdit(obj.description if obj else "")

        self.amount_spin = _amount_spinbox(obj.amount if obj else 0.0)
        self.amount_spin.setRange(0, 1_000_000)

        self.category_combo = QComboBox()
        self.category_combo.setEditable(True)
        categories = sorted(session.query(Category).all(), key=lambda c: c.name)
        for c in categories:
            self.category_combo.addItem(c.name)
        if obj:
            self.category_combo.setCurrentText(obj.category.name)

        self.account_combo = QComboBox()
        accounts = sorted(session.query(Account).all(), key=lambda a: a.name)
        for a in accounts:
            self.account_combo.addItem(a.name, a.id)
        if obj:
            self.account_combo.setCurrentIndex(self.account_combo.findData(obj.account_id))

        form = QFormLayout()
        form.addRow("Date", self.date_edit)
        form.addRow("Description", self.desc_edit)
        form.addRow("Amount", self.amount_spin)
        form.addRow("Category", self.category_combo)
        form.addRow("Account", self.account_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setLayout(form)

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

        category = get_or_create_category(self.session, category_name)
        account_id = self.account_combo.currentData()

        if self.obj is None:
            self.obj = UpcomingExpense(
                date=_to_pydate(self.date_edit.date()),
                description=description,
                amount=self.amount_spin.value(),
                category=category,
                account_id=account_id,
            )
            self.session.add(self.obj)
        else:
            self.obj.date = _to_pydate(self.date_edit.date())
            self.obj.description = description
            self.obj.amount = self.amount_spin.value()
            self.obj.category = category
            self.obj.account_id = account_id

        self.session.commit()
        self.accept()
