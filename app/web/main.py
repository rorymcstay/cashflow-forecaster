"""Entrypoint for the NiceGUI web front end: `uv run python -m app.web.main`.

Reuses the same SQLite DB and business logic (app/forecast.py, app/models.py)
as the desktop app — this is purely an alternative front end."""

import os

from nicegui import ui

from app.db import init_db
from app.web import _engineio_patch  # noqa: F401
from app.web.pages import (  # noqa: F401
    accounts,
    budget_builder,
    budget_items,
    budget_view,
    cashflow,
    dashboard,
    insights,
    investment_sim,
    scenarios,
    statements,
    transactions,
    upcoming_expenses,
    vendor_groups,
)

init_db()

ui.run(
    title="Budgeting",
    host="0.0.0.0",
    port=8084,
    reload=os.environ.get("RELOAD") == "1",
    # Default (3s) is too tight for a Swarm-deployed server — a brief network
    # blip or reconnect attempt easily exceeds it, and NiceGUI responds by
    # force-reloading the page (losing whatever you were doing) instead of
    # quietly resuming. See _engineio_patch.py for the related root cause.
    reconnect_timeout=60.0,
)
