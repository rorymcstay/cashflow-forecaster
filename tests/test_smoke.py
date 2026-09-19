"""Import-only checks that catch syntax errors and broken imports across the app."""

import importlib

MODULES = [
    "app.classify",
    "app.db",
    "app.forecast",
    "app.investment_sim",
    "app.investments",
    "app.main",
    "app.market_data",
    "app.mcp_server",
    "app.models",
    "app.pdf_statement_parsers",
    "app.scenario_sim",
    "app.seed",
    "app.statement_import",
    "app.statements",
    "app.transactions",
    "app.ui.accounts_screen",
    "app.ui.budget_items_screen",
    "app.ui.budget_view_screen",
    "app.ui.cashflow_screen",
    "app.ui.crud_screen",
    "app.ui.dashboard_screen",
    "app.ui.dialogs",
    "app.ui.filter_proxy",
    "app.ui.investment_sim_screen",
    "app.ui.scenario_screen",
    "app.ui.statements_screen",
    "app.ui.table_models",
    "app.ui.theme",
    "app.ui.transactions_screen",
    "app.ui.upcoming_expenses_screen",
    "app.ui.widgets",
    "app.web.layout",
    "app.web.pages.accounts",
    "app.web.pages.budget_items",
    "app.web.pages.budget_view",
    "app.web.pages.cashflow",
    "app.web.pages.dashboard",
    "app.web.pages.investment_sim",
    "app.web.pages.scenarios",
    "app.web.pages.statements",
    "app.web.pages.transactions",
    "app.web.pages.upcoming_expenses",
]


def test_modules_import():
    for name in MODULES:
        importlib.import_module(name)
