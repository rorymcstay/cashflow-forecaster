import datetime as dt

import plotly.graph_objects as go
from nicegui import ui

from app import investment_sim, market_data
from app.investment_sim import grid_values
from app.models import Account
from app.web.layout import ACCENT, TEXT_MUTED, get_page_session, page_shell

GRID_MODES = {"return_vol": "Return & Volatility", "contribution_horizon": "Contribution & Horizon"}


@ui.page("/investment-sim")
def investment_sim_page():
    session = get_page_session()

    with page_shell("/investment-sim", "Investment Simulation"):
        ui.label(
            "Historical-bootstrap Monte Carlo: resamples an account's actual historical monthly "
            "returns (via Yahoo Finance) rather than assuming a parametric distribution."
        ).style(f"color: {TEXT_MUTED};")

        accounts = [a for a in session.query(Account).order_by(Account.name).all() if a.holdings]
        account_options = {a.id: f"{a.name} ({', '.join(h.ticker for h in a.holdings)})" for a in accounts}

        with ui.row().classes("items-center gap-4 flex-wrap"):
            account_select = ui.select(
                account_options, label="Account", value=next(iter(account_options), None)
            )
            as_of_input = ui.input("As Of", value=dt.date.today().isoformat()).props("type=date")
            lookback_input = ui.number("Lookback (yrs)", value=10, min=1, max=50, format="%.0f")
            horizon_input = ui.number("Horizon (yrs)", value=20, min=1, max=60, format="%.0f")
            contribution_input = ui.number("Monthly Contribution", value=0.0, format="%.2f")
            force_refresh_check = ui.checkbox("Force refresh market data")

        with ui.row():
            ui.button("Run Simulation", on_click=lambda: run_base_simulation())

        status_label = ui.label("").style(f"color: {TEXT_MUTED}; white-space: pre-line;")
        plot = ui.plotly({}).classes("w-full")

        ui.label("Scenario Grid").classes("text-xl font-bold mt-2")
        with ui.row().classes("items-center gap-4"):
            grid_mode_select = ui.select(GRID_MODES, label="Scenario grid", value="return_vol")
            ui.button("Run Grid", on_click=lambda: run_grid())
        ui.label("Each cell: median ending balance (median worst peak-to-trough drawdown)").style(
            f"color: {TEXT_MUTED}; font-size: 11px;"
        )

        with (
            ui.row()
            .classes("gap-4 items-end")
            .bind_visibility_from(grid_mode_select, "value", backward=lambda v: v == "return_vol")
        ):
            with ui.column():
                ui.label("Return shift (% pts)").style(f"color: {TEXT_MUTED};")
                with ui.row().classes("gap-2"):
                    rs_min = ui.number("min", value=-4.0, step=1.0)
                    rs_max = ui.number("max", value=4.0, step=1.0)
                    rs_step = ui.number("step", value=2.0, step=0.5)
            with ui.column():
                ui.label("Vol scale (×)").style(f"color: {TEXT_MUTED};")
                with ui.row().classes("gap-2"):
                    vs_min = ui.number("min", value=0.5, step=0.1)
                    vs_max = ui.number("max", value=1.5, step=0.1)
                    vs_step = ui.number("step", value=0.25, step=0.05)

        with (
            ui.row()
            .classes("gap-4 items-end")
            .bind_visibility_from(grid_mode_select, "value", backward=lambda v: v == "contribution_horizon")
        ):
            with ui.column():
                ui.label("Monthly Contribution (£)").style(f"color: {TEXT_MUTED};")
                with ui.row().classes("gap-2"):
                    contrib_min = ui.number("min", value=0, step=100, format="%.0f")
                    contrib_max = ui.number("max", value=1000, step=100, format="%.0f")
                    contrib_step = ui.number("step", value=200, step=100, format="%.0f")
            with ui.column():
                ui.label("Horizon (years)").style(f"color: {TEXT_MUTED};")
                with ui.row().classes("gap-2"):
                    horizon_min = ui.number("min", value=5, step=1, format="%.0f")
                    horizon_max = ui.number("max", value=30, step=1, format="%.0f")
                    horizon_step = ui.number("step", value=5, step=1, format="%.0f")

        grid_table = ui.table(columns=[], rows=[], row_key="row_label").classes("w-full")

        def selected_account() -> Account | None:
            account_id = account_select.value
            return session.get(Account, account_id) if account_id is not None else None

        def historical_returns(account: Account, as_of: dt.date) -> list[float] | None:
            returns = market_data.fetch_portfolio_monthly_returns(
                account.portfolio_weights,
                as_of,
                int(lookback_input.value),
                force_refresh=force_refresh_check.value,
            )
            if not returns:
                ui.notify(
                    "Couldn't fetch historical price data for this portfolio "
                    "(bad ticker, no network, or too little history).",
                    type="negative",
                )
                return None
            return returns

        def run_base_simulation():
            account = selected_account()
            if account is None:
                ui.notify("Add holdings to an account first, on the Accounts page.", type="warning")
                return
            as_of = dt.date.fromisoformat(as_of_input.value)
            returns = historical_returns(account, as_of)
            if returns is None:
                return

            n_periods = int(horizon_input.value) * 12
            paths = investment_sim.bootstrap_paths(
                returns,
                account.current_balance,
                n_periods,
                n_paths=2000,
                monthly_contribution=contribution_input.value,
            )
            bands = investment_sim.percentile_bands(paths)
            years = [i / 12 for i in range(n_periods + 1)]

            fig = go.Figure()
            fig.add_trace(go.Scatter(x=years, y=bands[5], mode="lines", line=dict(width=0), showlegend=False))
            fig.add_trace(
                go.Scatter(
                    x=years,
                    y=bands[95],
                    mode="lines",
                    line=dict(width=0),
                    fill="tonexty",
                    fillcolor="rgba(91,141,239,0.15)",
                    name="5th–95th percentile",
                )
            )
            fig.add_trace(
                go.Scatter(x=years, y=bands[25], mode="lines", line=dict(width=0), showlegend=False)
            )
            fig.add_trace(
                go.Scatter(
                    x=years,
                    y=bands[75],
                    mode="lines",
                    line=dict(width=0),
                    fill="tonexty",
                    fillcolor="rgba(91,141,239,0.3)",
                    name="25th–75th percentile",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=years, y=bands[50], mode="lines", name="Median", line=dict(color=ACCENT, width=2.5)
                )
            )
            fig.update_layout(
                title="Projected Balance — 5th/25th/50th/75th/95th percentile",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=TEXT_MUTED),
                xaxis=dict(title="Years", gridcolor="#2C313C"),
                yaxis=dict(tickprefix="£", gridcolor="#2C313C"),
                margin=dict(l=10, r=10, t=40, b=10),
            )
            plot.figure = fig
            plot.update()

            mean_return = sum(returns) / len(returns)
            dd = investment_sim.drawdown_stats(paths)
            status_label.set_text(
                f"{len(returns)} historical monthly returns used (mean {mean_return * 100:.2f}%/mo, "
                f"~{((1 + mean_return) ** 12 - 1) * 100:.2f}%/yr). Median ending balance: "
                f"£{bands[50][-1]:,.0f} (90% range £{bands[5][-1]:,.0f} – £{bands[95][-1]:,.0f}).\n"
                f"Drawdown (worst peak-to-trough dip along the way): median {dd['percentiles'][50] * 100:.0f}%, "
                f"90% range {dd['percentiles'][5] * 100:.0f}% to {dd['percentiles'][95] * 100:.0f}%, "
                f"worst-case path {dd['worst'] * 100:.0f}%."
            )

        def populate_grid(
            grid: dict, row_values: list, col_values: list, corner_label: str, row_fmt, col_fmt
        ):
            columns = [
                {"name": "row_label", "label": corner_label, "field": "row_label", "align": "left"}
            ] + [
                {"name": f"col{i}", "label": col_fmt(cv), "field": f"col{i}", "align": "right"}
                for i, cv in enumerate(col_values)
            ]
            rows = []
            for rv in row_values:
                row = {"row_label": row_fmt(rv)}
                for i, cv in enumerate(col_values):
                    stats = grid.get((rv, cv))
                    row[f"col{i}"] = (
                        "—"
                        if stats is None
                        else f"£{stats['median_ending_balance']:,.0f} ({stats['median_max_drawdown'] * 100:.0f}%)"
                    )
                rows.append(row)
            grid_table.columns = columns
            grid_table.rows = rows
            grid_table.update()

        def run_grid():
            account = selected_account()
            if account is None:
                ui.notify("Add holdings to an account first, on the Accounts page.", type="warning")
                return
            as_of = dt.date.fromisoformat(as_of_input.value)
            returns = historical_returns(account, as_of)
            if returns is None:
                return

            if grid_mode_select.value == "return_vol":
                return_shifts = [v / 100 for v in grid_values(rs_min.value, rs_max.value, rs_step.value)]
                vol_scales = grid_values(vs_min.value, vs_max.value, vs_step.value)
                grid = investment_sim.return_vol_grid(
                    returns,
                    account.current_balance,
                    int(horizon_input.value) * 12,
                    return_shifts,
                    vol_scales,
                    monthly_contribution=contribution_input.value,
                )
                populate_grid(
                    grid,
                    return_shifts,
                    vol_scales,
                    "Return shift \\ Vol scale",
                    lambda v: f"{v * 100:+.1f}%",
                    lambda v: f"{v:.2f}×",
                )
            else:
                contributions = grid_values(contrib_min.value, contrib_max.value, contrib_step.value)
                horizons = [
                    int(v) for v in grid_values(horizon_min.value, horizon_max.value, horizon_step.value)
                ]
                grid = investment_sim.contribution_horizon_grid(
                    returns, account.current_balance, contributions, horizons
                )
                populate_grid(
                    grid,
                    contributions,
                    horizons,
                    "Contribution \\ Horizon",
                    lambda v: f"£{v:,.0f}/mo",
                    lambda v: f"{v} yrs",
                )
