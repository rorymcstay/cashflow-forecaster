import datetime as dt
from types import SimpleNamespace

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.budget_builder import SpendAggregate, aggregate_spend, vendor_options
from app.models import OCCURRENCES_PER_YEAR, Account, BudgetItem, Category, FlowType, Frequency
from app.seed import get_or_create_category
from app.transactions import query_transactions
from app.ui import theme
from app.ui.widgets import AccountMultiSelect


def _to_pydate(qd: QDate) -> dt.date:
    return dt.date(qd.year(), qd.month(), qd.day())


def _to_qdate(d: dt.date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _amount_spinbox(initial: float = 0.0) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setDecimals(2)
    box.setRange(0, 1_000_000)
    box.setSingleStep(1.0)
    box.setPrefix("£ ")
    box.setValue(initial)
    return box


class BudgetBuilderScreen(QWidget):
    """Suggested Budget Builder: aggregate historical spend by vendor and/or
    category across one or more source accounts, see what it averages out to
    per period, and commit that figure as a BudgetItem on any target account
    — e.g. Uber on the Amex averaged monthly, or Holiday spend across every
    account averaged every 6 months and applied to one card.

    This is the deliberate, user-directed counterpart to
    app/statement_import.py's automatic generate_suggestions.
    """

    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.on_change = on_change
        self._all_transactions: list = []
        self._vendors: list = []
        self._current_aggregate: SpendAggregate | None = None
        self._line_frequency: Frequency = Frequency.MONTHLY
        self._added_item_ids: set[int] = set()

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("<h2>Budget Builder</h2>"))
        description = QLabel(
            "Pick a vendor and/or category, narrow it to one or more source accounts, and see what it "
            "averages out to per period — then commit that figure as a budget line on any account."
        )
        description.setWordWrap(True)
        description.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        outer.addWidget(description)

        filters_row = QHBoxLayout()
        filters_row.addWidget(QLabel("Vendors:"))
        self.vendor_select = AccountMultiSelect(
            self, noun="vendor", noun_plural="vendors", all_selected_label="All Vendors (combined)"
        )
        filters_row.addWidget(self.vendor_select)

        filters_row.addWidget(QLabel("Category:"))
        self.category_combo = QComboBox()
        self.category_combo.addItem("Any category", None)
        for c in session.query(Category).order_by(Category.name).all():
            self.category_combo.addItem(c.name, c.id)
        filters_row.addWidget(self.category_combo)

        filters_row.addWidget(QLabel("Source accounts:"))
        self.source_account_select = AccountMultiSelect(self, all_selected_label="All Accounts (combined)")
        filters_row.addWidget(self.source_account_select)

        filters_row.addWidget(QLabel("Averaging interval:"))
        self.interval_combo = QComboBox()
        for f in Frequency:
            self.interval_combo.addItem(f.value, f)
        self.interval_combo.setCurrentIndex(self.interval_combo.findData(Frequency.MONTHLY))
        filters_row.addWidget(self.interval_combo)
        filters_row.addStretch()
        outer.addLayout(filters_row)

        range_row = QHBoxLayout()
        self.from_check = QCheckBox("From")
        self.from_edit = QDateEdit(_to_qdate(dt.date.today() - dt.timedelta(days=365)))
        self.from_edit.setCalendarPopup(True)
        self.from_edit.setEnabled(False)
        range_row.addWidget(self.from_check)
        range_row.addWidget(self.from_edit)
        self.to_check = QCheckBox("To")
        self.to_edit = QDateEdit(QDate.currentDate())
        self.to_edit.setCalendarPopup(True)
        self.to_edit.setEnabled(False)
        range_row.addWidget(self.to_check)
        range_row.addWidget(self.to_edit)
        hint = QLabel("Leave both unchecked to use the full history of whatever matches above.")
        hint.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        range_row.addWidget(hint)
        range_row.addStretch()
        outer.addLayout(range_row)

        # -- results / add-line split -------------------------------------------

        splitter = QSplitter(Qt.Orientation.Horizontal)

        results_scroll = QScrollArea()
        results_scroll.setWidgetResizable(True)
        results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        results_inner = QWidget()
        self.results_layout = QVBoxLayout(results_inner)
        results_scroll.setWidget(results_inner)
        splitter.addWidget(results_scroll)
        self._render_no_results("Choose a vendor and/or category to aggregate.")

        # -- right pane: add-as-budget-line form + full budget line list --------

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setMinimumWidth(340)
        right_inner = QFrame()
        right_inner.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        right_layout = QVBoxLayout(right_inner)

        self.add_form = QWidget()
        add_form_layout = QVBoxLayout(self.add_form)
        add_form_layout.setContentsMargins(0, 0, 0, 0)
        add_form_layout.addWidget(QLabel("<b>Add as Budget Line</b>"))
        add_form_layout.addWidget(QLabel("Description:"))
        self.desc_edit = QLineEdit()
        add_form_layout.addWidget(self.desc_edit)
        add_form_layout.addWidget(QLabel("Amount:"))
        self.amount_spin = _amount_spinbox()
        add_form_layout.addWidget(self.amount_spin)
        add_form_layout.addWidget(QLabel("Frequency:"))
        self.frequency_combo = QComboBox()
        for f in Frequency:
            self.frequency_combo.addItem(f.value, f)
        self.frequency_combo.setCurrentIndex(self.frequency_combo.findData(Frequency.MONTHLY))
        add_form_layout.addWidget(self.frequency_combo)
        add_form_layout.addWidget(QLabel("Type:"))
        self.flow_combo = QComboBox()
        for ft in FlowType:
            if ft != FlowType.TRANSFER:
                self.flow_combo.addItem(ft.value, ft)
        add_form_layout.addWidget(self.flow_combo)
        add_form_layout.addWidget(QLabel("Category:"))
        self.budget_category_combo = QComboBox()
        self.budget_category_combo.setEditable(True)
        for c in session.query(Category).order_by(Category.name).all():
            self.budget_category_combo.addItem(c.name)
        add_form_layout.addWidget(self.budget_category_combo)
        add_form_layout.addWidget(QLabel("Target account:"))
        self.target_account_combo = QComboBox()
        add_form_layout.addWidget(self.target_account_combo)
        add_form_layout.addWidget(QLabel("Effective From:"))
        self.effective_from_edit = QDateEdit(QDate.currentDate())
        self.effective_from_edit.setCalendarPopup(True)
        add_form_layout.addWidget(self.effective_from_edit)

        self.add_button = QPushButton("+ Add Budget Line")
        self.add_button.setObjectName("primaryButton")
        self.add_button.clicked.connect(self._add_budget_line)
        add_form_layout.addWidget(self.add_button)

        right_layout.addWidget(self.add_form)
        self.add_form.setVisible(False)

        right_layout.addWidget(QLabel("<b>All Budget Lines</b>"))
        self.all_lines_table = QTableWidget()
        self.all_lines_table.setColumnCount(6)
        self.all_lines_table.setHorizontalHeaderLabels(
            ["", "Description", "Category", "Amount", "Frequency", "Account"]
        )
        self.all_lines_table.horizontalHeader().setStretchLastSection(True)
        self.all_lines_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.all_lines_table.verticalHeader().setVisible(False)
        right_layout.addWidget(self.all_lines_table, stretch=1)

        right_scroll.setWidget(right_inner)
        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 420])
        outer.addWidget(splitter, stretch=1)

        self.vendor_select.selectionChanged.connect(self._recompute)
        self.category_combo.currentIndexChanged.connect(self._recompute)
        self.source_account_select.selectionChanged.connect(self._recompute)
        self.interval_combo.currentIndexChanged.connect(self._recompute)
        self.from_check.toggled.connect(self._on_range_toggled)
        self.to_check.toggled.connect(self._on_range_toggled)
        self.from_edit.dateChanged.connect(self._recompute)
        self.to_edit.dateChanged.connect(self._recompute)
        self.frequency_combo.currentIndexChanged.connect(self._on_line_frequency_changed)

        self.reload_accounts()
        self.refresh()

    # -- data --------------------------------------------------------------

    def reload_accounts(self):
        accounts = self.session.query(Account).order_by(Account.name).all()
        self.source_account_select.set_accounts(accounts)

        self.target_account_combo.blockSignals(True)
        current = self.target_account_combo.currentData()
        self.target_account_combo.clear()
        for a in accounts:
            self.target_account_combo.addItem(a.name, a.id)
        if current is not None:
            idx = self.target_account_combo.findData(current)
            if idx >= 0:
                self.target_account_combo.setCurrentIndex(idx)
        self.target_account_combo.blockSignals(False)

    def refresh(self):
        self._all_transactions = query_transactions(self.session)
        self._vendors = vendor_options(self._all_transactions)
        vendor_items = [
            SimpleNamespace(id=v.key, name=f"{v.label} ({v.transaction_count})") for v in self._vendors
        ]
        self.vendor_select.set_accounts(vendor_items, default_all_checked=False)
        self._recompute()
        self._refresh_budget_lines()

    # -- filters -------------------------------------------------------------

    def _on_range_toggled(self):
        self.from_edit.setEnabled(self.from_check.isChecked())
        self.to_edit.setEnabled(self.to_check.isChecked())
        self._recompute()

    def _selected_date_range(self) -> tuple[dt.date | None, dt.date | None]:
        start = _to_pydate(self.from_edit.date()) if self.from_check.isChecked() else None
        end = _to_pydate(self.to_edit.date()) if self.to_check.isChecked() else None
        return start, end

    # -- results --------------------------------------------------------------

    def _clear_results(self):
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _render_no_results(self, message: str):
        self._clear_results()
        label = QLabel(message)
        label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.results_layout.addWidget(label)
        self.results_layout.addStretch()

    def _build_tile(self, title: str, value: str, color: str | None = None) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        v = QVBoxLayout(frame)
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        value_label = QLabel(value)
        value_label.setStyleSheet(f"color: {color or theme.TEXT}; font-size: 20px; font-weight: 700;")
        v.addWidget(title_label)
        v.addWidget(value_label)
        return frame

    def _recompute(self):
        vendor_keys = self.vendor_select.checked_ids() or None
        category_id = self.category_combo.currentData()
        interval = self.interval_combo.currentData()

        if not vendor_keys and category_id is None:
            self.add_form.setVisible(False)
            self._current_aggregate = None
            self._render_no_results("Choose a vendor and/or category to aggregate.")
            return

        start_date, end_date = self._selected_date_range()
        source_account_ids = self.source_account_select.checked_ids() or None

        base = query_transactions(
            self.session,
            account_ids=source_account_ids,
            category_ids=[category_id] if category_id is not None else None,
            start_date=start_date,
            end_date=end_date,
        )
        aggregate = aggregate_spend(
            base, interval, vendor_keys=vendor_keys, start_date=start_date, end_date=end_date
        )
        self._current_aggregate = aggregate

        self._clear_results()
        if not aggregate.transactions:
            self._render_no_results("No matching transactions for this combination.")
            self.add_form.setVisible(False)
            return

        tiles_row = QHBoxLayout()
        tiles_row.addWidget(self._build_tile("Matched transactions", str(len(aggregate.transactions))))
        window_text = (
            f"{aggregate.start_date:%d %b %Y} – {aggregate.end_date:%d %b %Y}"
            if aggregate.start_date and aggregate.end_date
            else "—"
        )
        tiles_row.addWidget(self._build_tile("Window", window_text))
        tiles_row.addWidget(
            self._build_tile(
                "Total", f"£{aggregate.total:,.2f}", theme.WARNING if aggregate.total < 0 else theme.SUCCESS
            )
        )
        tiles_row.addWidget(
            self._build_tile(
                f"Average per {self.interval_combo.currentText()}",
                f"£{aggregate.average_per_period:,.2f}",
                theme.WARNING if aggregate.average_per_period < 0 else theme.SUCCESS,
            )
        )
        self.results_layout.addLayout(tiles_row)

        self.results_layout.addWidget(QLabel(f"<b>Matching transactions ({len(aggregate.transactions)})</b>"))
        preview_table = QTableWidget()
        preview_table.setColumnCount(4)
        preview_table.setHorizontalHeaderLabels(["Date", "Account", "Description", "Amount"])
        preview_table.horizontalHeader().setStretchLastSection(True)
        preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        preview_table.verticalHeader().setVisible(False)
        preview_rows = aggregate.transactions[:200]
        preview_table.setRowCount(len(preview_rows))
        for row, t in enumerate(preview_rows):
            preview_table.setItem(row, 0, QTableWidgetItem(t.date.strftime("%d %b %Y")))
            preview_table.setItem(row, 1, QTableWidgetItem(t.statement.account.name))
            preview_table.setItem(row, 2, QTableWidgetItem(t.description))
            amount_item = QTableWidgetItem(f"£{t.amount:,.2f}")
            amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            preview_table.setItem(row, 3, amount_item)
        preview_table.resizeColumnsToContents()
        preview_table.setMinimumHeight(260)
        self.results_layout.addWidget(preview_table)

        # Prefill the add-line form from this aggregate — a fresh group each
        # time you recompute, so overwriting is the expected result.
        vendor_labels = [v.label for v in self._vendors if v.key in (vendor_keys or [])]
        vendor_label = " + ".join(vendor_labels) if vendor_labels else None
        category_label = self.category_combo.currentText() if category_id is not None else None
        self.desc_edit.setText(vendor_label or category_label or "")
        self.amount_spin.setValue(abs(aggregate.average_per_period))
        # The committed budget line's frequency defaults to whatever interval
        # was just used to average history, but is independently editable
        # below (_on_line_frequency_changed rescales the amount rather than
        # re-running the whole aggregate) — e.g. average annual holiday spend
        # but commit it as a smoothed monthly line.
        self.frequency_combo.blockSignals(True)
        idx = self.frequency_combo.findData(interval)
        if idx >= 0:
            self.frequency_combo.setCurrentIndex(idx)
        self.frequency_combo.blockSignals(False)
        self._line_frequency = interval
        self.flow_combo.setCurrentIndex(
            self.flow_combo.findData(
                FlowType.INCOME if aggregate.average_per_period > 0 else FlowType.EXPENSE
            )
        )
        if category_label:
            self.budget_category_combo.setCurrentText(category_label)
        elif aggregate.transactions[0].category:
            self.budget_category_combo.setCurrentText(aggregate.transactions[0].category.name)
        if source_account_ids and len(source_account_ids) == 1:
            idx = self.target_account_combo.findData(source_account_ids[0])
            if idx >= 0:
                self.target_account_combo.setCurrentIndex(idx)
        self.add_form.setVisible(True)

    def _on_line_frequency_changed(self):
        new_freq = self.frequency_combo.currentData()
        if new_freq is None or new_freq == self._line_frequency:
            return
        current_amount = self.amount_spin.value()
        if current_amount:
            rescaled = (
                current_amount * OCCURRENCES_PER_YEAR[self._line_frequency] / OCCURRENCES_PER_YEAR[new_freq]
            )
            self.amount_spin.setValue(round(rescaled, 2))
        self._line_frequency = new_freq

    # -- add as budget line -----------------------------------------------------

    def _add_budget_line(self):
        if self._current_aggregate is None or not self._current_aggregate.transactions:
            QMessageBox.warning(self, "Nothing to add", "Adjust the filters above first.")
            return
        description = self.desc_edit.text().strip()
        if not description:
            QMessageBox.warning(self, "Missing description", "Please enter a description.")
            return
        category_name = self.budget_category_combo.currentText().strip()
        if not category_name:
            QMessageBox.warning(self, "Missing category", "Please enter or choose a category.")
            return
        target_account_id = self.target_account_combo.currentData()
        if target_account_id is None:
            QMessageBox.warning(self, "Missing account", "Choose a target account.")
            return

        line_frequency = self.frequency_combo.currentData()
        item = BudgetItem(
            description=description,
            amount=abs(self.amount_spin.value()),
            flow_type=self.flow_combo.currentData(),
            frequency=line_frequency,
            effective_from=_to_pydate(self.effective_from_edit.date()),
            category=get_or_create_category(self.session, category_name),
            account_id=target_account_id,
        )
        self.session.add(item)
        self.session.commit()

        self._added_item_ids.add(item.id)
        self._refresh_budget_lines()

        if self.on_change:
            self.on_change()

    def _refresh_budget_lines(self):
        items = self.session.query(BudgetItem).all()
        items.sort(key=lambda i: (i.id not in self._added_item_ids, i.account.name, i.description))
        self.all_lines_table.setRowCount(0)
        for item in items:
            row = self.all_lines_table.rowCount()
            self.all_lines_table.insertRow(row)
            is_new = item.id in self._added_item_ids
            status_item = QTableWidgetItem("New" if is_new else "")
            if is_new:
                status_item.setForeground(QColor(theme.ACCENT))
                font = status_item.font()
                font.setBold(True)
                status_item.setFont(font)
            self.all_lines_table.setItem(row, 0, status_item)
            self.all_lines_table.setItem(row, 1, QTableWidgetItem(item.description))
            self.all_lines_table.setItem(
                row, 2, QTableWidgetItem(item.category.name if item.category else "—")
            )
            amount_item = QTableWidgetItem(f"£{item.amount:,.2f}")
            amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.all_lines_table.setItem(row, 3, amount_item)
            self.all_lines_table.setItem(row, 4, QTableWidgetItem(item.frequency.value))
            self.all_lines_table.setItem(row, 5, QTableWidgetItem(item.account.name))
        self.all_lines_table.resizeColumnsToContents()
