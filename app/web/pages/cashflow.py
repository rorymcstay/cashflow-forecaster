import datetime as dt

import plotly.graph_objects as go
from nicegui import ui

from app import investments
from app.forecast import (
    account_daily_forecast,
    bucket_date_ranges,
    combined_daily_forecast,
    low_balance_warnings,
)
from app.models import Account
from app.web.layout import ACCENT, TEXT_MUTED, WARNING, get_page_session, page_shell

CHART_FREQUENCIES = ["Daily", "Weekly", "Monthly"]
ACCOUNT_COLORS = ["#5B8DEF", "#34D399", "#F2555C", "#F2B705", "#B45BEF", "#05C7F2", "#F2905B", "#8D95A3"]


def _money(value: float | None) -> str:
    return f"£{value:,.2f}" if value is not None else "—"


@ui.page("/cashflow")
def cashflow_page():
    session = get_page_session()

    with page_shell("/cashflow", "Cash Flow Forecast"):
        accounts = session.query(Account).order_by(Account.name).all()
        account_options = {a.id: a.name for a in accounts}
        today = dt.date.today()
        month_start = today.replace(day=1)
        month_end = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)

        with ui.tabs().classes("w-full") as tabs:
            cashflow_tab = ui.tab("Cashflow")
            investments_tab = ui.tab("Investments")

        with ui.tab_panels(tabs, value=cashflow_tab).classes("w-full"):
            with ui.tab_panel(cashflow_tab):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    start_input = ui.input("Start", value=month_start.isoformat()).props("type=date")
                    end_input = ui.input("End", value=month_end.isoformat()).props("type=date")
                    account_select = (
                        ui.select(
                            account_options,
                            multiple=True,
                            label="Accounts",
                            value=list(account_options.keys()),
                        )
                        .classes("min-w-[260px]")
                        .props("clearable")
                    )
                    freq_select = ui.select(CHART_FREQUENCIES, label="Chart points", value="Daily")
                    split_check = ui.checkbox("Split chart by account")
                    rebase_check = ui.checkbox("Rebase to 0 (change since start)")
                    ui.button("Refresh", on_click=lambda: render())

                warnings_label = ui.label().style("white-space: pre-line;")
                plot = ui.plotly({}).classes("w-full")
                table = ui.table(
                    columns=[
                        {"name": "date", "label": "Date", "field": "date", "align": "left"},
                        {"name": "in", "label": "Forecast In", "field": "in_", "align": "right"},
                        {"name": "out", "label": "Forecast Out", "field": "out", "align": "right"},
                        {"name": "net", "label": "Net", "field": "net", "align": "right"},
                        {"name": "balance", "label": "Balance", "field": "balance", "align": "right"},
                        {"name": "details", "label": "Details", "field": "details", "align": "left"},
                    ],
                    rows=[],
                    row_key="date",
                    pagination=25,
                ).classes("w-full")

                def render():
                    range_start = dt.date.fromisoformat(start_input.value)
                    range_end = dt.date.fromisoformat(end_input.value)
                    if range_end < range_start:
                        warnings_label.set_text("End date is before start date.")
                        warnings_label.style(f"color: {WARNING};")
                        table.rows = []
                        table.update()
                        plot.figure = go.Figure()
                        plot.update()
                        return

                    selected_ids = list(account_select.value or [])
                    selected_accounts = [a for a in accounts if a.id in selected_ids]
                    if not selected_accounts:
                        warnings_label.set_text("Select at least one account.")
                        warnings_label.style(f"color: {WARNING};")
                        table.rows = []
                        table.update()
                        plot.figure = go.Figure()
                        plot.update()
                        return

                    single_account = len(selected_accounts) == 1
                    split_mode = not single_account and split_check.value
                    account = selected_accounts[0] if single_account else None
                    if single_account:
                        df = account_daily_forecast(session, account, range_start, range_end)
                    else:
                        df = combined_daily_forecast(
                            session, range_start, range_end, accounts=selected_accounts
                        )

                    if df.height == 0:
                        warnings_label.set_text(
                            "No data for this range — the account's Balance As Of date is after the "
                            "selected range."
                        )
                        warnings_label.style(f"color: {WARNING};")
                        table.rows = []
                        table.update()
                        plot.figure = go.Figure()
                        plot.update()
                        return

                    rows = list(df.iter_rows(named=True))
                    dates = [r["date"] for r in rows]
                    balances = [r["balance"] for r in rows]

                    table.rows = [
                        {
                            "date": r["date"].strftime("%a %d %b %Y"),
                            "in_": _money(r["in"]),
                            "out": _money(r["out"]),
                            "net": _money(r["net"]),
                            "balance": _money(r["balance"]),
                            "details": r.get("details") or "",
                        }
                        for r in rows
                    ]
                    table.update()

                    freq = freq_select.value or "Daily"
                    bucket_ranges = bucket_date_ranges(dates, freq)
                    chart_dates = [dates[end] for _, end in bucket_ranges]
                    chart_balances = [balances[end] for _, end in bucket_ranges]
                    rebase = rebase_check.value
                    threshold = account.low_balance_threshold if single_account and account else None

                    fig = go.Figure()
                    if split_mode:
                        for i, acc in enumerate(selected_accounts):
                            if acc.name not in df.columns:
                                continue
                            col = df[acc.name].to_list()
                            base = col[0] if rebase else 0.0
                            y = [col[end] - base for _, end in bucket_ranges]
                            fig.add_trace(
                                go.Scatter(
                                    x=chart_dates,
                                    y=y,
                                    mode="lines",
                                    name=acc.name,
                                    line=dict(color=ACCOUNT_COLORS[i % len(ACCOUNT_COLORS)], width=2.5),
                                )
                            )
                    else:
                        if rebase:
                            base = balances[0]
                            chart_balances = [b - base for b in chart_balances]
                            if threshold is not None:
                                threshold -= base
                        fig.add_trace(
                            go.Scatter(
                                x=chart_dates,
                                y=chart_balances,
                                mode="lines",
                                name="Balance",
                                line=dict(color=ACCENT, width=2.5),
                            )
                        )
                        if threshold is not None:
                            fig.add_hline(y=threshold, line=dict(color=WARNING, dash="dash"))
                    fig.add_hline(y=0, line=dict(color=TEXT_MUTED, dash="dot", width=1))

                    fig.update_layout(
                        title="Change Since Start" if rebase else "Balance",
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(0,0,0,0)",
                        font=dict(color=TEXT_MUTED),
                        yaxis=dict(tickprefix="£", gridcolor="#2C313C"),
                        xaxis=dict(gridcolor="#2C313C"),
                        margin=dict(l=10, r=10, t=40, b=10),
                        showlegend=split_mode,
                    )
                    plot.figure = fig
                    plot.update()

                    warnings = low_balance_warnings(
                        session, range_start, range_end, accounts=selected_accounts
                    )
                    if warnings:
                        shown = warnings[:8]
                        lines = [
                            f"⚠ {w['account']} drops below {_money(w['threshold'])} on "
                            f"{w['date'].strftime('%d %b %Y')} (forecast {_money(w['balance'])})"
                            for w in shown
                        ]
                        if len(warnings) > len(shown):
                            lines.append(f"…and {len(warnings) - len(shown)} more in this range")
                        warnings_label.set_text("\n".join(lines))
                        warnings_label.style(f"color: {WARNING}; font-weight: 600; white-space: pre-line;")
                    else:
                        warnings_label.set_text("")

                for control in (
                    start_input,
                    end_input,
                    account_select,
                    freq_select,
                    split_check,
                    rebase_check,
                ):
                    control.on_value_change(lambda e: render())
                render()

            with ui.tab_panel(investments_tab):
                with ui.row().classes("items-center gap-2"):
                    ui.button("Sync Investment Prices", on_click=lambda: sync_investments()).props("outline")
                inv_summary_label = ui.label().style(f"color: {TEXT_MUTED}; white-space: pre-line;")
                inv_placeholder = ui.label(
                    "No investment accounts yet — add holdings to an account on the Accounts page."
                ).style(f"color: {TEXT_MUTED};")
                inv_table = ui.table(
                    columns=[
                        {"name": "account", "label": "Account", "field": "account", "align": "left"},
                        {"name": "ticker", "label": "Ticker", "field": "ticker", "align": "left"},
                        {"name": "quantity", "label": "Quantity", "field": "quantity", "align": "right"},
                        {"name": "avg_price", "label": "Avg Price", "field": "avg_price", "align": "right"},
                        {"name": "price", "label": "Current Price", "field": "price", "align": "right"},
                        {
                            "name": "cost_basis",
                            "label": "Cost Basis",
                            "field": "cost_basis",
                            "align": "right",
                        },
                        {"name": "value", "label": "Market Value", "field": "value", "align": "right"},
                        {"name": "gain", "label": "Unrealized G/L", "field": "gain", "align": "right"},
                        {"name": "gain_pct", "label": "G/L %", "field": "gain_pct", "align": "right"},
                    ],
                    rows=[],
                    row_key="row_key",
                ).classes("w-full")

                def render_investments():
                    inv_accounts = [
                        a for a in session.query(Account).order_by(Account.name).all() if a.share_quantities
                    ]
                    inv_placeholder.set_visibility(not inv_accounts)
                    inv_table.set_visibility(bool(inv_accounts))

                    total_cash = sum(a.cash_position for a in inv_accounts)
                    total_balance = sum(a.current_balance for a in inv_accounts)
                    total_cost_basis = 0.0
                    total_gain = 0.0
                    any_cost_basis = False
                    rows = []
                    for account in inv_accounts:
                        for h in investments.holdings_detail(account):
                            rows.append(
                                {
                                    "row_key": f"{account.id}-{h['ticker']}",
                                    "account": account.name,
                                    "ticker": h["ticker"],
                                    "quantity": f"{h['quantity']:g}",
                                    "avg_price": _money(h["average_price"]),
                                    "price": _money(h["price"]),
                                    "cost_basis": _money(h["cost_basis"]),
                                    "value": _money(h["value"]),
                                    "gain": _money(h["unrealized_gain"]),
                                    "gain_pct": (
                                        f"{h['unrealized_gain_pct'] * 100:+.1f}%"
                                        if h["unrealized_gain_pct"] is not None
                                        else "—"
                                    ),
                                }
                            )
                            if h["cost_basis"] is not None:
                                total_cost_basis += h["cost_basis"]
                                any_cost_basis = True
                            if h["unrealized_gain"] is not None:
                                total_gain += h["unrealized_gain"]
                    inv_table.rows = rows
                    inv_table.update()

                    lines = [f"Cash: {_money(total_cash)}    Total balance: {_money(total_balance)}"]
                    if any_cost_basis:
                        gain_pct = (
                            f" ({total_gain / total_cost_basis * 100:+.1f}%)" if total_cost_basis else ""
                        )
                        lines.append(
                            "Unrealized gain/loss (priced holdings with an avg price set): "
                            f"{_money(total_gain)}{gain_pct}"
                        )
                    else:
                        lines.append(
                            "Set an avg price on a holding (Accounts page) to see unrealized gain/loss here."
                        )
                    inv_summary_label.set_text("\n".join(lines))

                def sync_investments():
                    inv_accounts = [a for a in session.query(Account).all() if a.holdings]
                    if not inv_accounts:
                        ui.notify("No account has holdings to sync yet.", type="warning")
                        return
                    failed = [
                        a.name
                        for a in inv_accounts
                        if investments.refresh_investment_value(session, a) is None
                    ]
                    render_investments()
                    if failed:
                        ui.notify(f"Couldn't fetch prices for: {', '.join(failed)}.", type="warning")
                    else:
                        ui.notify(f"Refreshed {len(inv_accounts)} investment account(s).", type="positive")

                render_investments()
