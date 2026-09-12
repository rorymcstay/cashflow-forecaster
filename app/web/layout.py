"""Shared dark theme + top nav, applied identically on every page — mirrors
the desktop app's one committed dark theme (app/ui/theme.py) so the two
front ends feel like the same product."""

from contextlib import contextmanager

from nicegui import ui
from sqlalchemy.orm import Session

from app.db import get_session

BG = "#14161B"
SURFACE = "#1B1E25"
BORDER = "#2C313C"
TEXT = "#EDEFF2"
TEXT_MUTED = "#8D95A3"
ACCENT = "#5B8DEF"
WARNING = "#F2555C"
SUCCESS = "#34D399"

NAV_ITEMS = [
    ("/", "Dashboard"),
    ("/accounts", "Accounts"),
    ("/budget-items", "Budget Items"),
    ("/upcoming-expenses", "Upcoming Expenses"),
    ("/statements", "Statements"),
    ("/transactions", "Transactions"),
    ("/budget", "Budget"),
    ("/cashflow", "Cash Flow Forecast"),
    ("/investment-sim", "Investment Simulation"),
]

# Registered once at import time with shared=True, so NiceGUI injects it into
# every page's <head> — not per-page-visit, which is what a naive "only call
# this once" guard would (wrongly) do, since each `@ui.page` route is its own
# HTML document: the first page visited in the process would get the theme,
# and every other page/subsequent navigation would silently render unstyled.
ui.add_head_html(
    f"""
    <style>
        body {{ background-color: {BG}; color: {TEXT}; }}
        .nav-link {{ color: {TEXT_MUTED}; text-decoration: none; padding: 4px 10px; border-radius: 6px; }}
        .nav-link:hover {{ color: {TEXT}; background-color: {SURFACE}; }}
        .nav-link-active {{ color: {TEXT} !important; background-color: {SURFACE}; font-weight: 600; }}
        .stat-card {{ background-color: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 12px 16px; }}
        .section-card {{ background-color: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 16px; }}
    </style>
    """,
    shared=True,
)


def _apply_theme() -> None:
    """Dark mode + Quasar color overrides are per-client state, so unlike the
    head CSS above these must be (re-)applied on every page visit."""
    ui.dark_mode().enable()
    ui.colors(primary=ACCENT, positive=SUCCESS, negative=WARNING)


def get_page_session() -> Session:
    """A SQLAlchemy session scoped to this browser tab: opened fresh on page
    load, closed when the tab disconnects (closed/navigated away/reloaded).
    Without this, each `@ui.page` visit would leak a pooled connection
    forever, eventually exhausting the pool under normal use."""
    session = get_session()
    ui.context.client.on_disconnect(session.close)
    return session


@contextmanager
def page_shell(active_path: str, title: str):
    _apply_theme()
    with (
        ui.header()
        .classes("items-center")
        .style(f"background-color: {SURFACE}; border-bottom: 1px solid {BORDER};")
    ):
        ui.label("Budgeting").classes("text-lg font-bold").style(f"color: {TEXT};")
        with ui.row().classes("gap-1 ml-6"):
            for path, label in NAV_ITEMS:
                classes = "nav-link" + (" nav-link-active" if path == active_path else "")
                ui.link(label, path).classes(classes)
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4") as content:
        ui.label(title).classes("text-2xl font-bold").style(f"color: {TEXT};")
        yield content
