import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.db import get_session, init_db
from app.seed import seed_defaults
from app.ui import theme
from app.ui.accounts_screen import AccountsScreen
from app.ui.budget_items_screen import BudgetItemsScreen
from app.ui.budget_view_screen import BudgetViewScreen
from app.ui.cashflow_screen import CashflowForecastScreen
from app.ui.statements_screen import StatementsScreen
from app.ui.upcoming_expenses_screen import UpcomingExpensesScreen


def _build_header_bar() -> QWidget:
    bar = QWidget()
    bar.setObjectName("HeaderBar")
    layout = QHBoxLayout(bar)
    layout.setContentsMargins(20, 12, 20, 12)
    layout.setSpacing(10)

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
    return bar


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Household Budgeting")
        self.setWindowIcon(QIcon(theme.ICON_PATH))
        self.resize(1150, 720)

        self.session = get_session()

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(_build_header_bar())

        self.tabs = QTabWidget()
        central_layout.addWidget(self.tabs)
        self.setCentralWidget(central)

        self.accounts_screen = AccountsScreen(self.session, on_change=self.on_data_changed)
        self.budget_items_screen = BudgetItemsScreen(self.session, on_change=self.on_data_changed)
        self.upcoming_screen = UpcomingExpensesScreen(self.session, on_change=self.on_data_changed)
        self.statements_screen = StatementsScreen(self.session, on_change=self.on_data_changed)
        self.budget_view_screen = BudgetViewScreen(self.session)
        self.cashflow_screen = CashflowForecastScreen(self.session)

        self.tabs.addTab(self.accounts_screen, "Accounts")
        self.tabs.addTab(self.budget_items_screen, "Budget Items")
        self.tabs.addTab(self.upcoming_screen, "Upcoming Expenses")
        self.tabs.addTab(self.statements_screen, "Statements")
        self.tabs.addTab(self.budget_view_screen, "Budget")
        self.tabs.addTab(self.cashflow_screen, "Cash Flow Forecast")

        self.tabs.currentChanged.connect(self.on_tab_changed)

    def on_data_changed(self):
        self.statements_screen.reload_accounts()
        self.statements_screen.refresh()
        self.budget_view_screen.refresh()
        self.cashflow_screen.reload_accounts()
        self.cashflow_screen.refresh()

    def on_tab_changed(self, index: int):
        widget = self.tabs.widget(index)
        if widget is self.statements_screen:
            self.statements_screen.reload_accounts()
            self.statements_screen.refresh()
        elif widget is self.budget_view_screen:
            self.budget_view_screen.refresh()
        elif widget is self.cashflow_screen:
            self.cashflow_screen.reload_accounts()
            self.cashflow_screen.refresh()

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
