"""Import-only checks that catch syntax errors and broken imports across the app."""

import importlib

MODULES = [
    "app.classify",
    "app.db",
    "app.forecast",
    "app.main",
    "app.mcp_server",
    "app.models",
    "app.seed",
    "app.statement_import",
    "app.statements",
    "app.ui.accounts_screen",
    "app.ui.budget_items_screen",
    "app.ui.budget_view_screen",
    "app.ui.cashflow_screen",
    "app.ui.crud_screen",
    "app.ui.dialogs",
    "app.ui.filter_proxy",
    "app.ui.statements_screen",
    "app.ui.table_models",
    "app.ui.theme",
    "app.ui.upcoming_expenses_screen",
]


def test_modules_import():
    for name in MODULES:
        importlib.import_module(name)
