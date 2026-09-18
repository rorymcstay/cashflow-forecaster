"""Shared dark theme + top nav, applied identically on every page — mirrors
the desktop app's one committed dark theme (app/ui/theme.py) so the two
front ends feel like the same product."""

from contextlib import contextmanager

from nicegui import ui
from sqlalchemy.orm import Session

from app.budgets import get_active_budget, list_budgets, set_active_budget
from app.db import get_session

BG = "#14161B"
SURFACE = "#1B1E25"
SURFACE_ALT = "#21252E"
BORDER = "#2C313C"
TEXT = "#EDEFF2"
TEXT_MUTED = "#8D95A3"
ACCENT = "#5B8DEF"
WARNING = "#F2555C"
SUCCESS = "#34D399"

NAV_ITEMS = [
    ("/", "Dashboard"),
    ("/accounts", "Accounts"),
    ("/cashflow", "Cash Flow Forecast"),
    ("/insights", "Insights"),
    ("/scenarios", "Scenarios"),
    ("/budget-items", "Budget Items"),
    ("/budget-builder", "Budget Builder"),
    ("/vendor-groups", "Vendor Groups"),
    ("/budgets", "Budgets"),
    ("/upcoming-expenses", "Upcoming Expenses"),
    ("/statements", "Statements"),
    ("/transactions", "Transactions"),
    ("/budget", "Budget"),
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
        .nav-link {{
            color: {TEXT_MUTED}; text-decoration: none; padding: 10px 16px; border-radius: 6px;
            display: block;
        }}
        .nav-link:hover {{ color: {TEXT}; background-color: {SURFACE_ALT}; }}
        .nav-link-active {{
            color: {TEXT} !important; background-color: {SURFACE_ALT}; font-weight: 600;
            border-left: 3px solid {ACCENT};
        }}
        .stat-card {{ background-color: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 12px 16px; }}
        .section-card {{ background-color: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 16px; }}
    </style>
    """,
    shared=True,
)


# Applied once at import time (class-level defaults, not per-client state)
# so every input/select/number field across every page renders at Quasar's
# "dense" density — noticeably tighter filter bars without touching every
# page's individual widget calls.
ui.input.default_props("dense")
ui.select.default_props("dense")
ui.number.default_props("dense")


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

    drawer = ui.left_drawer(value=True, bordered=True).style(
        f"background-color: {SURFACE}; border-right: 1px solid {BORDER};"
    )
    with drawer:
        ui.label("Budgeting").classes("text-lg font-bold px-4 pt-2 pb-1").style(f"color: {TEXT};")
        for path, label in NAV_ITEMS:
            classes = "nav-link" + (" nav-link-active" if path == active_path else "")
            ui.link(label, path).classes(classes)

    budget_session = get_session()
    try:
        budget_options = {b.id: b.name for b in list_budgets(budget_session, include_archived=False)}
        active_budget_id = get_active_budget(budget_session).id
    finally:
        budget_session.close()

    def on_budget_change(e) -> None:
        if e.value == active_budget_id:
            return
        session = get_session()
        try:
            set_active_budget(session, e.value)
            session.commit()
        finally:
            session.close()
        ui.navigate.reload()

    with (
        ui.header()
        .classes("items-center")
        .style(f"background-color: {SURFACE}; border-bottom: 1px solid {BORDER};")
    ):
        ui.button(icon="menu", on_click=drawer.toggle).props("flat round dense color=white")
        ui.label(title).classes("text-lg font-bold").style(f"color: {TEXT};")
        ui.space()
        ui.label("Budget:").style(f"color: {TEXT_MUTED};")
        ui.select(budget_options, value=active_budget_id, on_change=on_budget_change).props(
            "dense options-dense"
        ).style("min-width: 160px;")

    with ui.column().classes("w-full max-w-6xl mx-auto p-2 sm:p-4 gap-4") as content:
        yield content
