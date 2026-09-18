import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.budgets import get_active_budget, list_budgets, set_active_budget
from app.db import get_session, init_db
from app.seed import seed_defaults
from app.ui import theme
from app.ui.accounts_screen import AccountsScreen
from app.ui.budget_builder_screen import BudgetBuilderScreen
from app.ui.budget_items_screen import BudgetItemsScreen
from app.ui.budget_view_screen import BudgetViewScreen
from app.ui.budgets_screen import BudgetsScreen
from app.ui.cashflow_screen import CashflowForecastScreen
from app.ui.dashboard_screen import DashboardScreen
from app.ui.insights_screen import InsightsScreen
from app.ui.investment_sim_screen import InvestmentSimScreen
from app.ui.scenario_screen import ScenarioScreen
from app.ui.statements_screen import StatementsScreen
from app.ui.transactions_screen import TransactionsScreen
from app.ui.upcoming_expenses_screen import UpcomingExpensesScreen
from app.ui.vendor_groups_screen import VendorGroupsScreen

SIDEBAR_WIDTH = 200


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Household Budgeting")
        self.setWindowIcon(QIcon(theme.ICON_PATH))
        self.resize(1150, 720)

        self.session = get_session()
        self._sidebar_visible = True

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._build_header_bar())

        self.dashboard_screen = DashboardScreen(self.session)
        self.accounts_screen = AccountsScreen(self.session, on_change=self.on_data_changed)
        self.budget_items_screen = BudgetItemsScreen(self.session, on_change=self.on_data_changed)
        self.budget_builder_screen = BudgetBuilderScreen(self.session, on_change=self.on_data_changed)
        self.upcoming_screen = UpcomingExpensesScreen(self.session, on_change=self.on_data_changed)
        self.statements_screen = StatementsScreen(self.session, on_change=self.on_data_changed)
        self.transactions_screen = TransactionsScreen(self.session)
        self.budget_view_screen = BudgetViewScreen(self.session)
        self.cashflow_screen = CashflowForecastScreen(self.session)
        self.scenario_screen = ScenarioScreen(self.session)
        self.investment_sim_screen = InvestmentSimScreen(self.session)
        self.vendor_groups_screen = VendorGroupsScreen(self.session, on_change=self.on_data_changed)
        self.insights_screen = InsightsScreen(self.session)
        self.budgets_screen = BudgetsScreen(self.session, on_change=self._on_budgets_changed)

        self._screens = [
            (self.dashboard_screen, "Dashboard"),
            (self.cashflow_screen, "Cash Flow Forecast"),
            (self.insights_screen, "Insights"),
            (self.scenario_screen, "Scenarios"),
            (self.budget_items_screen, "Budget Items"),
            (self.budget_builder_screen, "Budget Builder"),
            (self.vendor_groups_screen, "Vendor Groups"),
            (self.upcoming_screen, "Upcoming Expenses"),
            (self.statements_screen, "Statements"),
            (self.transactions_screen, "Transactions"),
            (self.budget_view_screen, "Budget"),
            (self.investment_sim_screen, "Investment Simulation"),
        ]
        # Pinned to the bottom of the sidebar, below a separator — account
        # and budget switching are cross-cutting concerns you reach for
        # from anywhere, not just another screen in the main list.
        self._pinned_screens = [
            (self.accounts_screen, "Accounts"),
            (self.budgets_screen, "Budgets"),
        ]

        self.stack = QStackedWidget()
        for screen, _ in self._screens + self._pinned_screens:
            self.stack.addWidget(screen)

        self.nav_list = QListWidget()
        self.nav_list.setObjectName("SidebarNav")
        for _, title in self._screens:
            self.nav_list.addItem(title)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet(f"background-color: {theme.BORDER}; max-height: 1px; border: none;")

        self.pinned_nav_list = QListWidget()
        self.pinned_nav_list.setObjectName("SidebarNav")
        self.pinned_nav_list.setFrameShape(QFrame.Shape.NoFrame)
        for _, title in self._pinned_screens:
            self.pinned_nav_list.addItem(title)
        self.pinned_nav_list.setFixedHeight(
            self.pinned_nav_list.sizeHintForRow(0) * len(self._pinned_screens) + 4
        )

        # Both lists (and the initial selection below) must exist before
        # either signal is connected — _on_main_nav_changed/_on_pinned_nav_changed
        # each reach across to the other list, which doesn't exist yet
        # while the lists are still being constructed above.
        self.nav_list.setCurrentRow(0)
        self.nav_list.currentRowChanged.connect(self._on_main_nav_changed)
        self.pinned_nav_list.currentRowChanged.connect(self._on_pinned_nav_changed)

        sidebar = QWidget()
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)
        sidebar_layout.addWidget(self.nav_list, stretch=1)
        sidebar_layout.addWidget(separator)
        sidebar_layout.addWidget(self.pinned_nav_list)
        self.sidebar = sidebar

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.sidebar)
        body.addWidget(self.stack, stretch=1)
        central_layout.addLayout(body)

        self.setCentralWidget(central)

        self.stack.currentChanged.connect(self.on_screen_changed)
        self._reload_budget_select()

    def _build_header_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(10)

        self.sidebar_toggle_btn = QPushButton("☰")
        self.sidebar_toggle_btn.setObjectName("SidebarToggle")
        self.sidebar_toggle_btn.setFixedSize(32, 32)
        self.sidebar_toggle_btn.clicked.connect(self._toggle_sidebar)
        layout.addWidget(self.sidebar_toggle_btn)

        logo = QLabel()
        logo.setPixmap(
            QPixmap(theme.ICON_PATH).scaled(
                28, 28, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )
        layout.addWidget(logo)

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        title = QLabel("Household Budgeting")
        title.setObjectName("HeaderTitle")
        subtitle = QLabel("Accounts, recurring bills, and cash flow — all in one place")
        subtitle.setObjectName("HeaderSubtitle")
        text_col.addWidget(title)
        text_col.addWidget(subtitle)
        layout.addLayout(text_col)

        layout.addStretch()

        layout.addWidget(QLabel("Budget:"))
        self.budget_select = QComboBox()
        self.budget_select.setMinimumWidth(160)
        self.budget_select.currentIndexChanged.connect(self._on_budget_select_changed)
        layout.addWidget(self.budget_select)

        return bar

    def _toggle_sidebar(self):
        self._sidebar_visible = not self._sidebar_visible
        self.sidebar.setVisible(self._sidebar_visible)

    def _on_main_nav_changed(self, row: int):
        if row < 0:
            return
        self.pinned_nav_list.blockSignals(True)
        self.pinned_nav_list.setCurrentRow(-1)
        self.pinned_nav_list.blockSignals(False)
        self.stack.setCurrentIndex(row)

    def _on_pinned_nav_changed(self, row: int):
        if row < 0:
            return
        self.nav_list.blockSignals(True)
        self.nav_list.setCurrentRow(-1)
        self.nav_list.blockSignals(False)
        self.stack.setCurrentIndex(len(self._screens) + row)

    def _reload_budget_select(self):
        active = get_active_budget(self.session)
        self.budget_select.blockSignals(True)
        self.budget_select.clear()
        for budget in list_budgets(self.session, include_archived=False):
            self.budget_select.addItem(budget.name, budget.id)
        idx = self.budget_select.findData(active.id)
        if idx >= 0:
            self.budget_select.setCurrentIndex(idx)
        self.budget_select.blockSignals(False)

    def _on_budget_select_changed(self, _index: int):
        budget_id = self.budget_select.currentData()
        if budget_id is None or budget_id == get_active_budget(self.session).id:
            return
        set_active_budget(self.session, budget_id)
        self.session.commit()
        self.on_data_changed()

    def _on_budgets_changed(self):
        self.on_data_changed()

    def on_data_changed(self):
        self.dashboard_screen.reload_accounts()
        self.dashboard_screen.refresh()
        self.statements_screen.reload_accounts()
        self.statements_screen.refresh()
        self.transactions_screen.reload_accounts()
        self.transactions_screen.refresh()
        self.budget_builder_screen.reload_accounts()
        self.budget_builder_screen.refresh()
        self.budget_view_screen.reload_accounts()
        self.budget_view_screen.refresh()
        self.cashflow_screen.reload_accounts()
        self.cashflow_screen.refresh()
        self.scenario_screen.reload_accounts()
        self.investment_sim_screen.reload_accounts()
        self.insights_screen.reload()
        self._reload_budget_select()
        self.budgets_screen.refresh()

    def on_screen_changed(self, index: int):
        widget = self.stack.widget(index)
        if widget is self.dashboard_screen:
            self.dashboard_screen.reload_accounts()
            self.dashboard_screen.refresh()
        elif widget is self.statements_screen:
            self.statements_screen.reload_accounts()
            self.statements_screen.refresh()
        elif widget is self.transactions_screen:
            self.transactions_screen.reload_accounts()
            self.transactions_screen.refresh()
        elif widget is self.budget_builder_screen:
            self.budget_builder_screen.reload_accounts()
            self.budget_builder_screen.refresh()
        elif widget is self.budget_view_screen:
            self.budget_view_screen.reload_accounts()
            self.budget_view_screen.refresh()
        elif widget is self.cashflow_screen:
            self.cashflow_screen.reload_accounts()
            self.cashflow_screen.refresh()
        elif widget is self.scenario_screen:
            self.scenario_screen.reload_accounts()
        elif widget is self.investment_sim_screen:
            self.investment_sim_screen.reload_accounts()
        elif widget is self.insights_screen:
            self.insights_screen.reload()
        elif widget is self.budgets_screen:
            self.budgets_screen.refresh()

    def closeEvent(self, event):
        self.session.close()
        super().closeEvent(event)


def main():
    init_db()
    seed_session = get_session()
    seed_defaults(seed_session)
    seed_session.close()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(theme.build_palette())
    app.setStyleSheet(theme.STYLESHEET)
    app.setWindowIcon(QIcon(theme.ICON_PATH))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
