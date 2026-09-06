import datetime as dt

import plotly.graph_objects as go
from nicegui import ui

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


def _money(value: float) -> str:
    return f"£{value:,.2f}"


@ui.page("/cashflow")
def cashflow_page():
    session = get_page_session()

    with page_shell("/cashflow", "Cash Flow Forecast"):
        accounts = session.query(Account).order_by(Account.name).all()
        account_options = {a.id: a.name for a in accounts}
        today = dt.date.today()
        month_start = today.replace(day=1)
        month_end = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)

        with ui.row().classes("items-center gap-4 flex-wrap"):
            start_input = ui.input("Start", value=month_start.isoformat()).props("type=date")
            end_input = ui.input("End", value=month_end.isoformat()).props("type=date")
            account_select = ui.select(
                account_options, multiple=True, label="Accounts", value=list(account_options.keys())
            ).classes("min-w-[260px]")
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
                df = combined_daily_forecast(session, range_start, range_end, accounts=selected_accounts)

            if df.height == 0:
                warnings_label.set_text(
                    "No data for this range — the account's Balance As Of date is after the selected range."
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

            warnings = low_balance_warnings(session, range_start, range_end, accounts=selected_accounts)
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

        for control in (start_input, end_input, account_select, freq_select, split_check, rebase_check):
            control.on_value_change(lambda e: render())
        render()
