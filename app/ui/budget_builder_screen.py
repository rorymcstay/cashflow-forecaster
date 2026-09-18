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

from app.budget_builder import (
    SpendAggregate,
    StagedLine,
    aggregate_spend,
    commit_staged_line,
    uncaptured_by_vendor,
    uncaptured_transactions,
    vendor_options,
)
from app.budget_recommender import recommend_budget
from app.budgets import active_budget_items
from app.models import OCCURRENCES_PER_YEAR, Account, BudgetItem, Category, FlowType, Frequency
from app.transactions import query_transactions
from app.ui import theme
from app.ui.widgets import AccountMultiSelect

_UNCAPTURED_LIMIT = 30
_PREVIEW_LIMIT = 200


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
    per period, and stage that figure as a budget line on any target account
    — e.g. Uber on the Amex averaged monthly, or Holiday spend across every
    account averaged every 6 months and applied to one card. Stage as many
    lines as you like, then save them all together.

    A single global date range drives every average computed here — whatever
    it's set to is what gets baked into every line you stage, so there's no
    risk of one line quietly using a different lookback than the next.

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
        self._staged_lines: list[StagedLine] = []
        self._account_names: dict[int, str] = {}

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("<h2>Budget Builder</h2>"))
        description = QLabel(
            "Pick a vendor and/or category, narrow it to one or more source accounts, and see what it "
            "averages out to per period — then stage it as a budget line on any account. Stage as many "
            "lines as you like and save them together."
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
        range_row.addWidget(QLabel("<b>Global date range</b> (used to compute every line below):"))
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

        recommend_row = QHBoxLayout()
        recommend_btn = QPushButton("🔮 Recommend Budget")
        recommend_btn.clicked.connect(self._recommend_budget)
        recommend_row.addWidget(recommend_btn)
        recommend_hint = QLabel(
            "Proposes a full slate of staged lines from transaction history — recurring bills, "
            "usage-based charges, and a category catch-all for the rest — for you to review/edit below. "
            "Only the Source accounts filter above applies to it."
        )
        recommend_hint.setWordWrap(True)
        recommend_hint.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        recommend_row.addWidget(recommend_hint, stretch=1)
        outer.addLayout(recommend_row)

        # -- results / staging split -------------------------------------------

        splitter = QSplitter(Qt.Orientation.Horizontal)

        results_scroll = QScrollArea()
        results_scroll.setWidgetResizable(True)
        results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        results_inner = QWidget()
        self.results_layout = QVBoxLayout(results_inner)
        results_scroll.setWidget(results_inner)
        splitter.addWidget(results_scroll)
        self._render_no_results("Choose a vendor and/or category to aggregate.")

        # -- right pane: add-line form, staged lines, existing budget items -----

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setMinimumWidth(360)
        right_inner = QFrame()
        right_inner.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        right_layout = QVBoxLayout(right_inner)

        self.add_form = QWidget()
        add_form_layout = QVBoxLayout(self.add_form)
        add_form_layout.setContentsMargins(0, 0, 0, 0)
        add_form_layout.addWidget(QLabel("<b>Stage as Budget Line</b>"))
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

        self.vendor_scope_label = QLabel("")
        self.vendor_scope_label.setWordWrap(True)
        self.vendor_scope_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        add_form_layout.addWidget(self.vendor_scope_label)

        self.stage_button = QPushButton("+ Stage Budget Line")
        self.stage_button.setObjectName("primaryButton")
        self.stage_button.clicked.connect(self._stage_line)
        add_form_layout.addWidget(self.stage_button)

        right_layout.addWidget(self.add_form)
        self.add_form.setVisible(False)

        right_layout.addWidget(QLabel("<b>Staged Lines (not yet saved)</b>"))
        self.staged_table = QTableWidget()
        self.staged_table.setColumnCount(8)
        self.staged_table.setHorizontalHeaderLabels(
            ["Description", "Vendors", "Category", "Amount", "Frequency", "Account", "Window", ""]
        )
        self.staged_table.horizontalHeader().setStretchLastSection(False)
        self.staged_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.staged_table.verticalHeader().setVisible(False)
        right_layout.addWidget(self.staged_table)

        self.save_staged_button = QPushButton("Save All Staged Lines")
        self.save_staged_button.clicked.connect(self._save_staged_lines)
        self.save_staged_button.setEnabled(False)
        right_layout.addWidget(self.save_staged_button)

        right_layout.addWidget(QLabel("<b>Existing Budget Items</b>"))
        self.all_lines_table = QTableWidget()
        self.all_lines_table.setColumnCount(6)
        self.all_lines_table.setHorizontalHeaderLabels(
            ["Description", "Vendors", "Category", "Amount", "Frequency", "Account"]
        )
        self.all_lines_table.horizontalHeader().setStretchLastSection(True)
        self.all_lines_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.all_lines_table.verticalHeader().setVisible(False)
        right_layout.addWidget(self.all_lines_table, stretch=1)

        right_scroll.setWidget(right_inner)
        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 460])
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
        self._account_names = {a.id: a.name for a in accounts}
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
        self._refresh_existing_items()

    # -- filters -------------------------------------------------------------

    def _on_range_toggled(self):
        self.from_edit.setEnabled(self.from_check.isChecked())
        self.to_edit.setEnabled(self.to_check.isChecked())
        self._recompute()

    def _selected_date_range(self) -> tuple[dt.date | None, dt.date | None]:
        start = _to_pydate(self.from_edit.date()) if self.from_check.isChecked() else None
        end = _to_pydate(self.to_edit.date()) if self.to_check.isChecked() else None
        return start, end

    def _select_vendor(self, vendor_key: str):
        """Called from the uncaptured-spend table: narrow the vendor filter
        to just this one and recompute, so the next stage is a click away."""
        self.vendor_select.set_checked_ids([vendor_key])

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
            self._render_uncaptured(base)
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
        preview_rows = aggregate.transactions[:_PREVIEW_LIMIT]
        preview_table.setRowCount(len(preview_rows))
        for row, t in enumerate(preview_rows):
            preview_table.setItem(row, 0, QTableWidgetItem(t.date.strftime("%d %b %Y")))
            preview_table.setItem(row, 1, QTableWidgetItem(t.statement.account.name))
            preview_table.setItem(row, 2, QTableWidgetItem(t.description))
            amount_item = QTableWidgetItem(f"£{t.amount:,.2f}")
            amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            preview_table.setItem(row, 3, amount_item)
        preview_table.resizeColumnsToContents()
        preview_table.setMinimumHeight(220)
        self.results_layout.addWidget(preview_table)

        self._render_uncaptured(base)

        # Prefill the stage form from this aggregate — a fresh group each
        # time you recompute, so overwriting is the expected result.
        vendor_labels = [v.label for v in self._vendors if v.key in (vendor_keys or [])]
        vendor_label = " + ".join(vendor_labels) if vendor_labels else None
        category_label = self.category_combo.currentText() if category_id is not None else None
        self.desc_edit.setText(vendor_label or category_label or "")
        self.amount_spin.setValue(abs(aggregate.average_per_period))
        # The staged line's frequency defaults to whatever interval was just
        # used to average history, but is independently editable below
        # (_on_line_frequency_changed rescales the amount rather than
        # re-running the whole aggregate) — e.g. average annual holiday spend
        # but stage it as a smoothed monthly line.
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

        if vendor_keys and category_id is not None:
            self.vendor_scope_label.setText(
                f"Scoped to {len(vendor_keys)} vendor(s) — other {category_label} transactions stay "
                "uncaptured."
            )
        elif vendor_keys:
            self.vendor_scope_label.setText(f"Scoped to {len(vendor_keys)} vendor(s), any category.")
        else:
            self.vendor_scope_label.setText(f"Whole category ({category_label}) — every vendor in it.")
        self.add_form.setVisible(True)

    def _render_uncaptured(self, base: list):
        """Below the matching-transactions preview: whichever of `base` (the
        same account/category/date scope, but *not* narrowed by the vendor
        filter) has no covering budget item yet — ranked by spend, so it
        doubles as a shortlist of what to build a line for next."""
        rows = uncaptured_by_vendor(uncaptured_transactions(self.session, base))
        self.results_layout.addWidget(QLabel(f"<b>Uncaptured spend in this scope ({len(rows)} vendors)</b>"))
        hint = QLabel("Double-click a row to filter the vendor picker above to it.")
        hint.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        self.results_layout.addWidget(hint)

        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Vendor", "Count", "Total"])
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        shown = rows[:_UNCAPTURED_LIMIT]
        table.setRowCount(len(shown))
        for row, r in enumerate(shown):
            key_item = QTableWidgetItem(r.label)
            key_item.setData(Qt.ItemDataRole.UserRole, r.key)
            table.setItem(row, 0, key_item)
            table.setItem(row, 1, QTableWidgetItem(str(r.transaction_count)))
            amount_item = QTableWidgetItem(f"£{r.total:,.2f}")
            amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            amount_item.setForeground(QColor(theme.WARNING if r.total < 0 else theme.SUCCESS))
            table.setItem(row, 2, amount_item)
        table.resizeColumnsToContents()
        table.setMinimumHeight(200)
        table.cellDoubleClicked.connect(
            lambda r, _c, t=table: self._select_vendor(t.item(r, 0).data(Qt.ItemDataRole.UserRole))
        )
        self.results_layout.addWidget(table)
        self.results_layout.addStretch()

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

    # -- staging -----------------------------------------------------------

    def _stage_line(self):
        if self._current_aggregate is None or not self._current_aggregate.transactions:
            QMessageBox.warning(self, "Nothing to stage", "Adjust the filters above first.")
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

        vendor_keys = self.vendor_select.checked_ids() or []
        vendor_labels = [v.label for v in self._vendors if v.key in vendor_keys]
        staged = StagedLine(
            description=description,
            amount=abs(self.amount_spin.value()),
            flow_type=self.flow_combo.currentData(),
            frequency=self.frequency_combo.currentData(),
            account_id=target_account_id,
            category_name=category_name,
            vendor_keys=vendor_keys,
            vendor_label=" + ".join(vendor_labels),
            effective_from=_to_pydate(self.effective_from_edit.date()),
            window_start=self._current_aggregate.start_date,
            window_end=self._current_aggregate.end_date,
        )
        self._staged_lines.append(staged)
        self._render_staged_table()

    def _remove_staged(self, index: int):
        if 0 <= index < len(self._staged_lines):
            del self._staged_lines[index]
        self._render_staged_table()

    def _render_staged_table(self):
        self.staged_table.setRowCount(len(self._staged_lines))
        for row, staged in enumerate(self._staged_lines):
            desc_item = QTableWidgetItem(("🔮 " if staged.rationale else "") + staged.description)
            if staged.rationale:
                desc_item.setToolTip(staged.rationale)
            self.staged_table.setItem(row, 0, desc_item)
            self.staged_table.setItem(row, 1, QTableWidgetItem(staged.vendor_label or "Whole category"))
            self.staged_table.setItem(row, 2, QTableWidgetItem(staged.category_name))
            amount_item = QTableWidgetItem(f"£{staged.amount:,.2f}")
            amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.staged_table.setItem(row, 3, amount_item)
            self.staged_table.setItem(row, 4, QTableWidgetItem(staged.frequency.value))
            self.staged_table.setItem(
                row, 5, QTableWidgetItem(self._account_names.get(staged.account_id, "—"))
            )
            window_text = (
                f"{staged.window_start:%d %b %y} – {staged.window_end:%d %b %y}"
                if staged.window_start and staged.window_end
                else "—"
            )
            self.staged_table.setItem(row, 6, QTableWidgetItem(window_text))
            remove_btn = QPushButton("Remove")
            remove_btn.clicked.connect(lambda _checked=False, i=row: self._remove_staged(i))
            self.staged_table.setCellWidget(row, 7, remove_btn)
        self.staged_table.resizeColumnsToContents()
        self.save_staged_button.setEnabled(bool(self._staged_lines))
        self.save_staged_button.setText(
            f"Save All Staged Lines ({len(self._staged_lines)})"
            if self._staged_lines
            else "Save All Staged Lines"
        )

    def _recommend_budget(self):
        if self._staged_lines:
            reply = QMessageBox.question(
                self,
                "Replace staged lines?",
                f"This will replace the {len(self._staged_lines)} line(s) already staged with fresh "
                "recommendations. Continue?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        account_ids = self.source_account_select.checked_ids() or None
        recommendations = recommend_budget(self.session, account_ids=account_ids)
        if not recommendations:
            QMessageBox.information(
                self, "Nothing to recommend", "No recurring bills or material uncaptured spend found."
            )
            return
        self._staged_lines = recommendations
        self._render_staged_table()

    def _save_staged_lines(self):
        if not self._staged_lines:
            return
        for staged in self._staged_lines:
            commit_staged_line(self.session, staged)
        self.session.commit()
        count = len(self._staged_lines)
        self._staged_lines = []
        self._render_staged_table()
        self._refresh_existing_items()
        QMessageBox.information(self, "Saved", f"Saved {count} budget line(s).")
        if self.on_change:
            self.on_change()

    # -- existing budget items -----------------------------------------------

    def _refresh_existing_items(self):
        items = (
            active_budget_items(self.session).order_by(BudgetItem.account_id, BudgetItem.description).all()
        )
        self.all_lines_table.setRowCount(len(items))
        for row, item in enumerate(items):
            self.all_lines_table.setItem(row, 0, QTableWidgetItem(item.description))
            self.all_lines_table.setItem(row, 1, QTableWidgetItem(", ".join(item.vendor_list) or "—"))
            self.all_lines_table.setItem(
                row, 2, QTableWidgetItem(item.category.name if item.category else "—")
            )
            amount_item = QTableWidgetItem(f"£{item.amount:,.2f}")
            amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.all_lines_table.setItem(row, 3, amount_item)
            self.all_lines_table.setItem(row, 4, QTableWidgetItem(item.frequency.value))
            self.all_lines_table.setItem(row, 5, QTableWidgetItem(item.account.name))
        self.all_lines_table.resizeColumnsToContents()
