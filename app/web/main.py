"""Entrypoint for the NiceGUI web front end: `uv run python -m app.web.main`.

Reuses the same SQLite DB and business logic (app/forecast.py, app/models.py)
as the desktop app — this is purely an alternative front end."""

from nicegui import ui

from app.db import init_db
from app.web.pages import (  # noqa: F401
    accounts,
    budget_items,
    budget_view,
    cashflow,
    dashboard,
    investment_sim,
    scenarios,
    statements,
    transactions,
    upcoming_expenses,
)

init_db()

ui.run(title="Budgeting", port=8080, reload=True)
