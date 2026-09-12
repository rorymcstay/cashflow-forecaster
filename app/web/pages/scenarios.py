import plotly.graph_objects as go
from nicegui import ui

from app.forecast import HypotheticalItem
from app.models import Account, FlowType, Frequency
from app.scenario_sim import run_scenario
from app.web.layout import ACCENT, TEXT_MUTED, get_page_session, page_shell

_FLOW_OPTIONS = {FlowType.EXPENSE.value: FlowType.EXPENSE, FlowType.INCOME.value: FlowType.INCOME}
_FREQUENCY_OPTIONS = {f.value: f for f in Frequency}
_OFFSET_COLORS = ["#5B8DEF", "#34D399", "#F2555C", "#F2A65B", "#B98CE0", "#4FD1C5"]


def _money(value: float) -> str:
    return f"£{value:,.2f}"


@ui.page("/scenarios")
def scenarios_page():
    session = get_page_session()

    with page_shell("/scenarios", "Scenarios"):
        ui.label(
            "Stress-test the cashflow forecast: random shocks, income growth, a one-time payment "
            "compared at different timings, hypothetical (never-saved) budget lines, and — for "
            "accounts with holdings — historical market-regime variation instead of a flat return."
        ).style(f"color: {TEXT_MUTED};")

        accounts = session.query(Account).order_by(Account.name).all()
        account_options = {a.id: a.name for a in accounts}

        with ui.row().classes("items-end gap-4 flex-wrap"):
            account_select = (
                ui.select(account_options, label="Accounts", multiple=True, value=list(account_options))
                .classes("min-w-[220px]")
                .props("use-chips")
            )
            horizon_input = ui.number("Horizon (years)", value=5, min=1, max=10, format="%.0f")
            paths_input = ui.number("Simulation paths", value=500, min=50, max=5000, format="%.0f")
            regimes_check = ui.checkbox("Use market-regime variation", value=True)

        ui.label("Income growth").classes("text-lg font-bold mt-2")
        income_growth_input = ui.number("Annual income growth (%)", value=0.0, format="%.2f").classes(
            "max-w-[220px]"
        )

        ui.label("Random shock").classes("text-lg font-bold mt-2")
        with ui.row().classes("items-end gap-4 flex-wrap"):
            shock_prob_input = ui.number("Probability per year (%)", value=0.0, format="%.1f")
            shock_amount_input = ui.number("Size (£)", value=0.0, format="%.2f")
            shock_account_select = ui.select(account_options, label="Hits which account")

        ui.label("One-time payment").classes("text-lg font-bold mt-2")
        with ui.row().classes("items-end gap-4 flex-wrap"):
            payment_amount_input = ui.number("Amount (£)", value=0.0, format="%.2f")
            payment_account_select = ui.select(account_options, label="From which account")
            payment_offsets_input = ui.input("Years from now (comma-separated)", value="1,2,3,4,5")
        ui.label("Leave amount at 0 to skip this comparison.").style(f"color: {TEXT_MUTED}; font-size: 11px;")

        ui.label("Hypothetical budget lines").classes("text-lg font-bold mt-2")
        ui.label('Never saved — for exploring "what if I added this" only.').style(
            f"color: {TEXT_MUTED}; font-size: 11px;"
        )
        extra_lines_container = ui.column().classes("w-full gap-2")
        extra_line_rows: list[dict] = []

        def add_extra_line():
            with extra_lines_container:
                row = ui.row().classes("items-end gap-2 flex-wrap")
            with row:
                desc = ui.input("Description").classes("min-w-[160px]")
                amount = ui.number("Amount (£)", value=0.0, format="%.2f")
                flow = ui.select(list(_FLOW_OPTIONS), label="Type", value=FlowType.EXPENSE.value)
                freq = ui.select(list(_FREQUENCY_OPTIONS), label="Frequency", value=Frequency.MONTHLY.value)
                acct = ui.select(account_options, label="Account")
                remove_btn = ui.button(icon="close").props("flat dense round")
            entry = {
                "row": row,
                "desc": desc,
                "amount": amount,
                "flow": flow,
                "freq": freq,
                "account": acct,
            }
            remove_btn.on_click(lambda: (extra_lines_container.remove(row), extra_line_rows.remove(entry)))
            extra_line_rows.append(entry)

        ui.button("+ Add line", on_click=add_extra_line).props("flat dense")

        ui.button("Run Simulation", on_click=lambda: run()).classes("mt-4").props("color=primary")

        status_label = ui.label("").style(f"color: {TEXT_MUTED}; white-space: pre-line;")
        plot = ui.plotly({}).classes("w-full")
        impact_table = ui.table(columns=[], rows=[]).classes("w-full mt-2")

        def build_extra_items() -> list[HypotheticalItem] | None:
            items = []
            for entry in extra_line_rows:
                if not entry["desc"].value or not entry["amount"].value or entry["account"].value is None:
                    continue
                items.append(
                    HypotheticalItem(
                        description=entry["desc"].value,
                        amount=float(entry["amount"].value),
                        flow_type=_FLOW_OPTIONS[entry["flow"].value],
                        frequency=_FREQUENCY_OPTIONS[entry["freq"].value],
                        account_id=entry["account"].value,
                    )
                )
            return items or None

        def parse_offsets(text: str) -> list[int]:
            offsets = []
            for part in text.split(","):
                part = part.strip()
                if part:
                    offsets.append(int(part))
            return offsets

        def run():
            account_ids = account_select.value or None
            if not account_ids:
                ui.notify("Choose at least one account.", type="negative")
                return

            one_time_payment = None
            if payment_amount_input.value:
                if payment_account_select.value is None:
                    ui.notify("Choose an account for the one-time payment.", type="negative")
                    return
                try:
                    offsets = parse_offsets(payment_offsets_input.value)
                except ValueError:
                    ui.notify("Year offsets must be whole numbers, comma-separated.", type="negative")
                    return
                if not offsets:
                    ui.notify("Enter at least one year offset for the one-time payment.", type="negative")
                    return
                one_time_payment = {
                    "amount": float(payment_amount_input.value),
                    "account_id": payment_account_select.value,
                    "year_offsets": offsets,
                }

            try:
                result = run_scenario(
                    session,
                    horizon_years=int(horizon_input.value),
                    account_ids=account_ids,
                    n_paths=int(paths_input.value),
                    income_growth_rate_pct=income_growth_input.value or 0.0,
                    shock_probability_per_year=shock_prob_input.value or 0.0,
                    shock_amount=shock_amount_input.value or 0.0,
                    shock_account_id=shock_account_select.value,
                    extra_budget_items=build_extra_items(),
                    one_time_payment=one_time_payment,
                    use_market_regimes=regimes_check.value,
                )
            except ValueError as exc:
                ui.notify(str(exc), type="negative")
                return

            years = [i / 12 for i in range(len(result["dates"]))]
            bands = result["baseline"]

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

            if result["payment_scenarios"]:
                for i, (offset, offset_bands) in enumerate(sorted(result["payment_scenarios"].items())):
                    color = _OFFSET_COLORS[i % len(_OFFSET_COLORS)]
                    fig.add_trace(
                        go.Scatter(
                            x=years,
                            y=offset_bands[50],
                            mode="lines",
                            name=f"Pay in year {offset}",
                            line=dict(color=color, width=2, dash="dot"),
                        )
                    )

            fig.update_layout(
                title="Combined Balance — 5th/25th/50th/75th/95th percentile",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=TEXT_MUTED),
                xaxis=dict(title="Years", gridcolor="#2C313C"),
                yaxis=dict(tickprefix="£", gridcolor="#2C313C"),
                margin=dict(l=10, r=10, t=40, b=10),
            )
            plot.figure = fig
            plot.update()

            summary = result["summary"]
            regime_note = (
                f"Market-regime data used for: {', '.join(result['regime_accounts_used'])}."
                if result["regime_accounts_used"]
                else "No market-regime data applied (no in-scope holdings, or data unavailable)."
            )
            status_label.set_text(
                f"Probability of dropping below £0 at some point: {summary['probability_below_zero'] * 100:.0f}%. "
                f"Median ending balance: {_money(summary['median_ending_balance'])}.\n{regime_note}"
            )

            if summary["payment_impact"]:
                impact_table.columns = [
                    {"name": "offset", "label": "Pay in year", "field": "offset", "align": "left"},
                    {
                        "name": "ending",
                        "label": "Median ending balance",
                        "field": "ending",
                        "align": "right",
                    },
                    {
                        "name": "vs_baseline",
                        "label": "Vs. no payment",
                        "field": "vs_baseline",
                        "align": "right",
                    },
                    {
                        "name": "prob_below_zero",
                        "label": "Prob. below £0",
                        "field": "prob_below_zero",
                        "align": "right",
                    },
                ]
                impact_table.rows = [
                    {
                        "offset": offset,
                        "ending": _money(impact["median_ending_balance"]),
                        "vs_baseline": _money(impact["vs_baseline_median"]),
                        "prob_below_zero": f"{impact['probability_below_zero'] * 100:.0f}%",
                    }
                    for offset, impact in sorted(summary["payment_impact"].items())
                ]
                impact_table.update()
            else:
                impact_table.columns = []
                impact_table.rows = []
                impact_table.update()
