import datetime as dt
from types import SimpleNamespace

from PySide6.QtCharts import QBarCategoryAxis, QBarSeries, QBarSet, QChart, QChartView, QValueAxis
from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import Session

from app.models import Account, Category, Transaction
from app.seed import get_or_create_category
from app.statement_import import UNCATEGORIZED, generate_suggestions, match_budget_item
from app.transactions import (
    DATE_RANGE_PRESETS,
    UNCATEGORIZED_ID,
    date_range_preset,
    merchant_key,
    query_transactions,
    recurring_groups_by_merchant,
    similar_transactions,
    spend_breakdown,
)
from app.ui import theme
from app.ui.filter_proxy import GroupFilterProxyModel, PageFilterProxyModel
from app.ui.table_models import ObjectTableModel
from app.ui.widgets import AccountMultiSelect

# (label, column index) options offered in the "Group by" combo — a subset of
# self.columns, since grouping by e.g. Description or Amount isn't useful.
_GROUP_OPTIONS = [("No grouping", -1), ("Category", 3), ("Account", 1), ("Merchant", 4), ("Month", 5)]

_PAGE_SIZES = [50, 100, 250, 500]
_CATEGORY_COLORS = ["#5B8DEF", "#34D399", "#F2555C", "#F2B705", "#B45BEF", "#05C7F2", "#F2905B", "#8D95A3"]


def _to_pydate(qd: QDate) -> dt.date:
    return dt.date(qd.year(), qd.month(), qd.day())


def _to_qdate(d: dt.date) -> QDate:
    return QDate(d.year, d.month, d.day)


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

        toolbar.addWidget(QLabel("Categories:"))
        self.category_select = AccountMultiSelect(
            self, noun="category", noun_plural="categories", all_selected_label="All Categories"
        )
        toolbar.addWidget(self.category_select)

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

        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Date range:"))
        self.range_combo = QComboBox()
        for key, label in DATE_RANGE_PRESETS.items():
            self.range_combo.addItem(label, key)
        range_row.addWidget(self.range_combo)
        range_row.addWidget(QLabel("From:"))
        self.from_edit = QDateEdit(_to_qdate(dt.date.today() - dt.timedelta(days=365)))
        self.from_edit.setCalendarPopup(True)
        self.from_edit.setEnabled(False)
        range_row.addWidget(self.from_edit)
        range_row.addWidget(QLabel("to"))
        self.to_edit = QDateEdit(QDate.currentDate())
        self.to_edit.setCalendarPopup(True)
        self.to_edit.setEnabled(False)
        range_row.addWidget(self.to_edit)
        range_row.addStretch()
        layout.addLayout(range_row)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, stretch=1)

        table_tab = QWidget()
        table_tab_layout = QVBoxLayout(table_tab)
        table_tab_layout.setContentsMargins(0, 8, 0, 0)
        self.tabs.addTab(table_tab, "Table")

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
        self.group_proxy = GroupFilterProxyModel(self)
        self.group_proxy.setSourceModel(self.model)
        self.page_proxy = PageFilterProxyModel(self)
        self.page_proxy.setSourceModel(self.group_proxy)

        self.table = QTableView()
        self.table.setModel(self.page_proxy)
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

        page_row = QHBoxLayout()
        page_row.addWidget(QLabel("Rows per page:"))
        self.page_size_combo = QComboBox()
        for size in _PAGE_SIZES:
            self.page_size_combo.addItem(str(size), size)
        # Combo's default selection must match PageFilterProxyModel's own
        # default page size — otherwise it displays "50" while actually
        # paginating at 100 until the user touches the control once.
        self.page_size_combo.setCurrentIndex(self.page_size_combo.findData(100))
        page_row.addWidget(self.page_size_combo)
        self.prev_page_btn = QPushButton("‹ Prev")
        self.prev_page_btn.clicked.connect(self._go_prev_page)
        page_row.addWidget(self.prev_page_btn)
        self.page_label = QLabel("Page 1 of 1")
        self.page_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        page_row.addWidget(self.page_label)
        self.next_page_btn = QPushButton("Next ›")
        self.next_page_btn.clicked.connect(self._go_next_page)
        page_row.addWidget(self.next_page_btn)
        page_row.addStretch()
        table_layout.addLayout(page_row)

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
        table_tab_layout.addWidget(splitter)

        self.tabs.addTab(self._build_summary_tab(), "Summary")

        self.table.selectionModel().selectionChanged.connect(self._update_selection_sum)
        self.account_select.selectionChanged.connect(self._reload_table)
        self.category_select.selectionChanged.connect(self._reload_table)
        self.filter_edit.textChanged.connect(self._on_filter_text_changed)
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        self.range_combo.currentIndexChanged.connect(self._on_range_preset_changed)
        self.from_edit.dateChanged.connect(self._reload_table)
        self.to_edit.dateChanged.connect(self._reload_table)
        self.page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)

        self._clear_detail()
        self.reload_accounts()
        self.refresh()

    # -- column helpers ------------------------------------------------------

    def _recurring_label(self, t: Transaction) -> str:
        group = self.recurring_groups.get(merchant_key(t))
        return group["guessed_frequency"] if group else ""

    def _amount_color(self, t: Transaction) -> str:
        return theme.WARNING if t.amount < 0 else theme.SUCCESS

    # -- summary tab -----------------------------------------------------------

    def _build_tile(self, title: str) -> tuple[QFrame, QLabel]:
        frame = QFrame()
        frame.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 8px;"
        )
        v = QVBoxLayout(frame)
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        value_label = QLabel("—")
        value_label.setStyleSheet(f"color: {theme.TEXT}; font-size: 22px; font-weight: 700;")
        v.addWidget(title_label)
        v.addWidget(value_label)
        return frame, value_label

    def _build_chart_view(self, title: str) -> tuple[QChart, QChartView]:
        chart = QChart()
        chart.setTitle(title)
        chart.setTitleBrush(QColor(theme.TEXT))
        chart.setBackgroundBrush(QColor(theme.SURFACE))
        chart.setBackgroundPen(QColor(theme.BORDER))
        chart.legend().setLabelColor(QColor(theme.TEXT_MUTED))
        chart_view = QChartView(chart)
        chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        chart_view.setMinimumHeight(320)
        return chart, chart_view

    def _style_axis(self, axis) -> None:
        axis.setLabelsColor(QColor(theme.TEXT_MUTED))
        axis.setGridLineColor(QColor(theme.BORDER))
        axis.setLinePenColor(QColor(theme.BORDER))
        if isinstance(axis, QBarCategoryAxis):
            font = axis.labelsFont()
            font.setPointSize(8)
            axis.setLabelsFont(font)

    def _build_summary_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        scroll.setWidget(inner)

        tiles_row = QHBoxLayout()
        income_frame, self.income_tile = self._build_tile("Total Income")
        expense_frame, self.expense_tile = self._build_tile("Total Expenses")
        net_frame, self.net_tile = self._build_tile("Net")
        avg_frame, self.avg_expense_tile = self._build_tile("Avg Monthly Spend")
        count_frame, self.count_tile = self._build_tile("Transactions")
        for frame in (income_frame, expense_frame, net_frame, avg_frame, count_frame):
            tiles_row.addWidget(frame)
        layout.addLayout(tiles_row)

        charts_row = QHBoxLayout()
        self.category_chart, category_chart_view = self._build_chart_view("Spend by Category")
        charts_row.addWidget(category_chart_view, stretch=1)

        merchants_col = QVBoxLayout()
        merchants_col.addWidget(QLabel("<b>Top Merchants</b>"))
        self.merchants_table = QTableWidget()
        self.merchants_table.setColumnCount(3)
        self.merchants_table.setHorizontalHeaderLabels(["Merchant", "Count", "Total"])
        self.merchants_table.horizontalHeader().setStretchLastSection(True)
        self.merchants_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.merchants_table.verticalHeader().setVisible(False)
        self.merchants_table.setMinimumHeight(280)
        merchants_col.addWidget(self.merchants_table)
        charts_row.addLayout(merchants_col, stretch=1)
        layout.addLayout(charts_row)

        self.monthly_chart, monthly_chart_view = self._build_chart_view("Income vs Expense by Month")
        layout.addWidget(monthly_chart_view)

        layout.addStretch()
        return scroll

    def _rebuild_category_chart(self, rows: list[dict]) -> None:
        self.category_chart.removeAllSeries()
        for axis in list(self.category_chart.axes()):
            self.category_chart.removeAxis(axis)
        if not rows:
            return

        bar_set = QBarSet("Spend")
        bar_set.append([r["amount"] for r in rows])
        series = QBarSeries()
        series.append(bar_set)
        series.setLabelsVisible(False)
        for i in range(len(rows)):
            bar_set.setColor(QColor(_CATEGORY_COLORS[i % len(_CATEGORY_COLORS)]))
        self.category_chart.addSeries(series)

        axis_x = QBarCategoryAxis()
        axis_x.append([r["category"] for r in rows])
        axis_x.setLabelsAngle(-45)
        self._style_axis(axis_x)
        self.category_chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)

        axis_y = QValueAxis()
        axis_y.setLabelFormat("£%.0f")
        max_amount = max((r["amount"] for r in rows), default=0.0)
        axis_y.setRange(0, max_amount * 1.15 if max_amount else 1)
        self._style_axis(axis_y)
        self.category_chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)

    def _rebuild_monthly_chart(self, rows: list[dict]) -> None:
        self.monthly_chart.removeAllSeries()
        for axis in list(self.monthly_chart.axes()):
            self.monthly_chart.removeAxis(axis)
        if not rows:
            return

        income_set = QBarSet("Income")
        income_set.append([r["income"] for r in rows])
        income_set.setColor(QColor(theme.SUCCESS))
        expense_set = QBarSet("Expense")
        expense_set.append([r["expense"] for r in rows])
        expense_set.setColor(QColor(theme.WARNING))
        series = QBarSeries()
        series.append(income_set)
        series.append(expense_set)
        self.monthly_chart.addSeries(series)
        self.monthly_chart.legend().setVisible(True)

        axis_x = QBarCategoryAxis()
        axis_x.append([r["month_label"] for r in rows])
        axis_x.setLabelsAngle(-45)
        self._style_axis(axis_x)
        self.monthly_chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)

        axis_y = QValueAxis()
        axis_y.setLabelFormat("£%.0f")
        max_amount = max((max(r["income"], r["expense"]) for r in rows), default=0.0)
        axis_y.setRange(0, max_amount * 1.15 if max_amount else 1)
        self._style_axis(axis_y)
        self.monthly_chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)

    def _rebuild_merchants_table(self, rows: list[dict]) -> None:
        self.merchants_table.setRowCount(0)
        for r in rows:
            row = self.merchants_table.rowCount()
            self.merchants_table.insertRow(row)
            self.merchants_table.setItem(row, 0, QTableWidgetItem(r["merchant"]))
            count_item = QTableWidgetItem(str(r["count"]))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.merchants_table.setItem(row, 1, count_item)
            amount_item = QTableWidgetItem(f"£{r['amount']:,.2f}")
            amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.merchants_table.setItem(row, 2, amount_item)
        self.merchants_table.resizeColumnToContents(0)

    def _refresh_summary(self, displayed: list[Transaction]) -> None:
        breakdown = spend_breakdown(displayed)
        self.income_tile.setText(f"£{breakdown['total_income']:,.2f}")
        self.expense_tile.setText(f"£{breakdown['total_expense']:,.2f}")
        self.net_tile.setText(f"£{breakdown['net']:,.2f}")
        net_color = theme.SUCCESS if breakdown["net"] >= 0 else theme.WARNING
        self.net_tile.setStyleSheet(f"color: {net_color}; font-size: 22px; font-weight: 700;")
        self.avg_expense_tile.setText(f"£{breakdown['avg_monthly_expense']:,.2f}")
        self.count_tile.setText(str(breakdown["transaction_count"]))

        self._rebuild_category_chart(breakdown["by_category"][:10])
        self._rebuild_monthly_chart(breakdown["by_month"])
        self._rebuild_merchants_table(breakdown["top_merchants"])

    # -- data ----------------------------------------------------------------

    def reload_accounts(self):
        self.account_select.set_accounts(self.session.query(Account).order_by(Account.name).all())
        categories: list = list(self.session.query(Category).order_by(Category.name).all())
        categories.append(SimpleNamespace(id=UNCATEGORIZED_ID, name="Uncategorized"))
        self.category_select.set_accounts(categories)

    def refresh(self):
        self._pool = query_transactions(self.session)
        self.recurring_groups = recurring_groups_by_merchant(self._pool)
        self._reload_table()

    def _selected_date_range(self) -> tuple[dt.date | None, dt.date | None]:
        key = self.range_combo.currentData()
        if key == "all":
            return None, None
        if key == "custom":
            return _to_pydate(self.from_edit.date()), _to_pydate(self.to_edit.date())
        return date_range_preset(key)

    def _reload_table(self):
        account_ids = self.account_select.checked_ids() or None
        category_ids = self.category_select.checked_ids() or None
        start_date, end_date = self._selected_date_range()
        displayed = query_transactions(
            self.session,
            account_ids=account_ids,
            category_ids=category_ids,
            start_date=start_date,
            end_date=end_date,
        )
        self.model.set_rows(displayed)
        self.table.resizeColumnsToContents()
        self._clear_detail()
        self.page_proxy.reset_page()
        self._update_pagination_controls()
        self._update_selection_sum()
        self._refresh_summary(displayed)

    def _on_filter_text_changed(self, text: str):
        self.group_proxy.set_filter_text(text)
        self.page_proxy.reset_page()
        self._update_pagination_controls()

    def _on_group_changed(self, _index: int):
        self.group_proxy.set_group_column(self.group_combo.currentData())
        self.page_proxy.reset_page()
        self._update_pagination_controls()
        self.table.viewport().update()

    def _on_range_preset_changed(self, _index: int):
        key = self.range_combo.currentData()
        is_custom = key == "custom"
        self.from_edit.setEnabled(is_custom)
        self.to_edit.setEnabled(is_custom)
        if not is_custom and key != "all":
            start, end = date_range_preset(key)
            if start is not None:
                self.from_edit.blockSignals(True)
                self.from_edit.setDate(_to_qdate(start))
                self.from_edit.blockSignals(False)
            if end is not None:
                self.to_edit.blockSignals(True)
                self.to_edit.setDate(_to_qdate(end))
                self.to_edit.blockSignals(False)
        self._reload_table()

    # -- pagination ------------------------------------------------------------

    def _update_pagination_controls(self):
        total = self.page_proxy.total_row_count()
        pages = self.page_proxy.page_count()
        page = self.page_proxy.page
        self.page_label.setText(f"Page {page + 1} of {pages} ({total} transactions)")
        self.prev_page_btn.setEnabled(page > 0)
        self.next_page_btn.setEnabled(page < pages - 1)

    def _on_page_size_changed(self, _index: int):
        self.page_proxy.set_page_size(self.page_size_combo.currentData())
        self._update_pagination_controls()

    def _go_prev_page(self):
        self.page_proxy.set_page(self.page_proxy.page - 1)
        self._update_pagination_controls()

    def _go_next_page(self):
        self.page_proxy.set_page(self.page_proxy.page + 1)
        self._update_pagination_controls()

    def _update_selection_sum(self, *_args):
        rows = self.table.selectionModel().selectedRows()
        transactions = [
            self.model.object_at(self.group_proxy.mapToSource(self.page_proxy.mapToSource(idx)).row())
            for idx in rows
        ]
        if not transactions:
            self.selection_label.setText("No rows selected")
            return
        total = sum(t.amount for t in transactions)
        self.selection_label.setText(f"{len(transactions)} selected · Sum £{total:,.2f}")

    def _on_row_clicked(self, index):
        source_row = self.group_proxy.mapToSource(self.page_proxy.mapToSource(index)).row()
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
