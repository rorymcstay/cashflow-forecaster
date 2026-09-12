from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.models import Account, Category, Transaction
from app.seed import get_or_create_category
from app.statement_import import UNCATEGORIZED, generate_suggestions, match_budget_item
from app.transactions import (
    merchant_key,
    query_transactions,
    recurring_groups_by_merchant,
    similar_transactions,
)
from app.ui import theme
from app.ui.filter_proxy import GroupFilterProxyModel
from app.ui.table_models import ObjectTableModel
from app.ui.widgets import AccountMultiSelect

# (label, column index) options offered in the "Group by" combo — a subset of
# self.columns, since grouping by e.g. Description or Amount isn't useful.
_GROUP_OPTIONS = [("No grouping", -1), ("Category", 3), ("Account", 1), ("Merchant", 4), ("Month", 5)]


class TransactionsScreen(QWidget):
    """Cross-account transaction browser: a filterable/groupable table with
    multi-row selection (summed at the bottom) on the left, and a detail
    pane on the right for reclassifying the clicked transaction and
    exploring its recurring/similar-merchant siblings.

    Merchant grouping/similarity reuses app/transactions.py, the same
    normalize_description/find_recurring_transactions logic that already
    seeds budget suggestions in app/statement_import.py.
    """

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self._pool: list[Transaction] = []
        self.recurring_groups: dict[str, dict] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Transactions</h2>"))

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Accounts:"))
        self.account_select = AccountMultiSelect(self)
        toolbar.addWidget(self.account_select)

        toolbar.addWidget(QLabel("Filter:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Type to filter…")
        self.filter_edit.setClearButtonEnabled(True)
        toolbar.addWidget(self.filter_edit, stretch=1)

        toolbar.addWidget(QLabel("Group by:"))
        self.group_combo = QComboBox()
        for label, col in _GROUP_OPTIONS:
            self.group_combo.addItem(label, col)
        toolbar.addWidget(self.group_combo)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self.columns = [
            ("Date", lambda t: t.date.strftime("%d %b %Y"), lambda t: t.date),
            ("Account", lambda t: t.statement.account.name),
            ("Description", lambda t: t.description),
            ("Category", lambda t: t.category.name if t.category else UNCATEGORIZED),
            ("Merchant", lambda t: merchant_key(t)),
            ("Month", lambda t: t.date.strftime("%B %Y"), lambda t: t.date.strftime("%Y-%m")),
            ("Recurring", self._recurring_label),
            ("Amount", lambda t: f"£{t.amount:,.2f}", lambda t: t.amount, self._amount_color),
        ]
        self.model = ObjectTableModel(self.columns)
        self.proxy = GroupFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.clicked.connect(self._on_row_clicked)
        table_layout.addWidget(self.table)

        self.selection_label = QLabel("No rows selected")
        self.selection_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        table_layout.addWidget(self.selection_label)
        splitter.addWidget(table_container)

        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setMinimumWidth(300)
        detail_inner = QFrame()
        detail_inner.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        self.detail_layout = QVBoxLayout(detail_inner)
        detail_scroll.setWidget(detail_inner)
        splitter.addWidget(detail_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([760, 320])
        layout.addWidget(splitter, stretch=1)

        self.table.selectionModel().selectionChanged.connect(self._update_selection_sum)
        self.account_select.selectionChanged.connect(self._reload_table)
        self.filter_edit.textChanged.connect(self.proxy.set_filter_text)
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)

        self._clear_detail()
        self.reload_accounts()
        self.refresh()

    # -- column helpers ------------------------------------------------------

    def _recurring_label(self, t: Transaction) -> str:
        group = self.recurring_groups.get(merchant_key(t))
        return group["guessed_frequency"] if group else ""

    def _amount_color(self, t: Transaction) -> str:
        return theme.WARNING if t.amount < 0 else theme.SUCCESS

    # -- data ----------------------------------------------------------------

    def reload_accounts(self):
        self.account_select.set_accounts(self.session.query(Account).order_by(Account.name).all())

    def refresh(self):
        self._pool = query_transactions(self.session)
        self.recurring_groups = recurring_groups_by_merchant(self._pool)
        self._reload_table()

    def _reload_table(self):
        account_ids = self.account_select.checked_ids() or None
        displayed = query_transactions(self.session, account_ids=account_ids)
        self.model.set_rows(displayed)
        self.table.resizeColumnsToContents()
        self._clear_detail()
        self._update_selection_sum()

    def _on_group_changed(self, _index: int):
        self.proxy.set_group_column(self.group_combo.currentData())
        self.table.viewport().update()

    def _update_selection_sum(self, *_args):
        rows = self.table.selectionModel().selectedRows()
        transactions = [self.model.object_at(self.proxy.mapToSource(idx).row()) for idx in rows]
        if not transactions:
            self.selection_label.setText("No rows selected")
            return
        total = sum(t.amount for t in transactions)
        self.selection_label.setText(f"{len(transactions)} selected · Sum £{total:,.2f}")

    def _on_row_clicked(self, index):
        source_row = self.proxy.mapToSource(index).row()
        self._show_detail(self.model.object_at(source_row))

    # -- detail pane ----------------------------------------------------------

    def _clear_layout(self):
        # hide() immediately, since takeAt() alone leaves a removed widget
        # visible at its old position (just unmanaged) until deleteLater()'s
        # deferred deletion actually runs on the next event loop turn.
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _clear_detail(self):
        self._clear_layout()
        placeholder = QLabel("Click a transaction to see details here.")
        placeholder.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        placeholder.setWordWrap(True)
        self.detail_layout.addWidget(placeholder)

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(f"<b>{text}</b>")
        label.setStyleSheet(f"margin-top: 8px; color: {theme.TEXT};")
        return label

    def _show_detail(self, transaction: Transaction):
        self._clear_layout()
        layout = self.detail_layout

        layout.addWidget(QLabel(f"<b>{transaction.date.strftime('%d %b %Y')}</b>"))
        desc_label = QLabel(transaction.description)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        amount_label = QLabel(f"£{transaction.amount:,.2f}")
        amount_label.setStyleSheet(
            f"color: {theme.WARNING if transaction.amount < 0 else theme.SUCCESS}; "
            "font-weight: bold; font-size: 14px;"
        )
        layout.addWidget(amount_label)
        account_label = QLabel(transaction.statement.account.name)
        account_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(account_label)

        layout.addWidget(self._section_label("Reclassify"))
        category_combo = QComboBox()
        category_combo.setEditable(True)
        categories = sorted({c.name for c in self.session.query(Category).all()})
        category_combo.addItems(categories)
        if transaction.category:
            category_combo.setCurrentText(transaction.category.name)
        layout.addWidget(category_combo)

        save_btn = QPushButton("Save category")
        save_btn.setObjectName("primaryButton")

        def save_category():
            name = category_combo.currentText().strip()
            if not name:
                QMessageBox.warning(self, "Missing category", "Please enter or choose a category.")
                return
            transaction.category = get_or_create_category(self.session, name)
            self.session.commit()
            self._reload_table()
            self._show_detail(transaction)

        save_btn.clicked.connect(save_category)
        layout.addWidget(save_btn)

        budget_item = transaction.matched_budget_item or match_budget_item(
            self.session, transaction.statement.account_id, transaction.description, transaction.date
        )
        budget_label = QLabel(f"Budget item: {budget_item.description if budget_item else '—'}")
        budget_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(budget_label)

        layout.addWidget(self._section_label("Recurring"))
        group = self.recurring_groups.get(merchant_key(transaction))
        if group is None:
            info = QLabel("No recurring pattern detected for this merchant yet.")
            info.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            info.setWordWrap(True)
            layout.addWidget(info)
        else:
            info = QLabel(
                f"{group['guessed_frequency']} · seen {group['occurrences']}x "
                f"({group['first_date']} – {group['last_date']}), avg £{group['avg_amount']:,.2f}"
            )
            info.setWordWrap(True)
            layout.addWidget(info)
            refresh_btn = QPushButton("Refresh budget suggestions from this")
            refresh_btn.clicked.connect(lambda: self._refresh_suggestions(transaction))
            layout.addWidget(refresh_btn)

        matches = similar_transactions(transaction, self._pool)
        layout.addWidget(self._section_label(f"Similar transactions ({len(matches)})"))
        if not matches:
            none_label = QLabel("No other transactions match this merchant.")
            none_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            layout.addWidget(none_label)
        for m in matches:
            btn = QPushButton(
                f"{m.date.strftime('%d %b %Y')}   {m.statement.account.name}   £{m.amount:,.2f}"
            )
            btn.setFlat(True)
            btn.setStyleSheet("text-align: left;")
            btn.clicked.connect(lambda _checked=False, m=m: self._show_detail(m))
            layout.addWidget(btn)

        layout.addStretch()

    def _refresh_suggestions(self, transaction: Transaction):
        generate_suggestions(self.session, transaction.statement.account)
        QMessageBox.information(
            self, "Suggestions refreshed", "See the Statements tab for updated budget suggestions."
        )
