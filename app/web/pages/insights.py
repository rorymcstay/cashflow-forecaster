import datetime as dt

import plotly.graph_objects as go
from nicegui import ui

from app.analytics import GRANULARITIES, expense_vs_forecast
from app.models import Account, Category
from app.vendor_groups import list_vendor_groups
from app.web.layout import TEXT_MUTED, get_page_session, page_shell

GROUP_BY_OPTIONS = ["Category", "Vendor Group"]

# Cycled per selected category/vendor group — line style (solid/dot/dash)
# is what tells Actual/Moving Average/Forecast apart, so color is free to
# identify which group a line belongs to.
SERIES_COLORS = ["#5B8DEF", "#34D399", "#F2555C", "#F2B705", "#B45BEF", "#05C7F2", "#F2905B", "#8D95A3"]


def _money(value: float) -> str:
    return f"£{value:,.2f}"


@ui.page("/insights")
def insights_page():
    session = get_page_session()

    with page_shell("/insights", "Insights"):
        ui.label(
            "Actual expense spend and its moving average, overlaid against the spend implied by "
            "matching budget items — grouped by Category or Vendor Group. Select multiple values to "
            "compare them on the same plot."
        ).style(f"color: {TEXT_MUTED};")

        accounts = session.query(Account).order_by(Account.name).all()
        account_options = {a.id: a.name for a in accounts}
        today = dt.date.today()
        value_label_by_id: dict[int, str] = {}

        with ui.expansion("Filters", value=True, icon="tune").classes("w-full section-card"):
            with ui.row().classes("items-center gap-2 flex-wrap"):
                group_by_select = ui.select(GROUP_BY_OPTIONS, label="Group by", value="Category")
                value_select = (
                    ui.select({}, multiple=True, label="Values (none = combined total)", value=[])
                    .classes("min-w-[260px]")
                    .props("clearable")
                )
                account_select = (
                    ui.select(
                        account_options, multiple=True, label="Accounts", value=list(account_options.keys())
                    )
                    .classes("min-w-[220px]")
                    .props("clearable")
                )

            with ui.row().classes("items-center gap-2 flex-wrap"):
                granularity_select = ui.select(GRANULARITIES, label="Granularity", value="Monthly")
                ma_input = ui.number("MA window", value=3, min=1, max=24, format="%.0f").classes("w-28")
                show_actual_check = ui.checkbox("Actual", value=True)
                show_ma_check = ui.checkbox("Moving Average", value=True)
                show_forecast_check = ui.checkbox("Forecast", value=True)

            with ui.row().classes("items-center gap-2 flex-wrap"):
                start_input = ui.input("Start", value=(today.replace(year=today.year - 1)).isoformat()).props(
                    "type=date"
                )
                end_input = ui.input("End", value=today.isoformat()).props("type=date")

        summary_label = ui.label().style(f"color: {TEXT_MUTED};")
        plot = ui.plotly({}).classes("w-full")
        table = ui.table(
            columns=[
                {"name": "group", "label": "Group", "field": "group", "align": "left"},
                {"name": "period", "label": "Period", "field": "period", "align": "left"},
                {"name": "actual", "label": "Actual", "field": "actual", "align": "right"},
                {"name": "ma", "label": "Moving Avg", "field": "ma", "align": "right"},
                {"name": "forecast", "label": "Forecast", "field": "forecast", "align": "right"},
            ],
            rows=[],
            row_key="row_key",
            pagination=25,
        ).classes("w-full")

        def refresh_value_options():
            nonlocal value_label_by_id
            current = [v for v in (value_select.value or []) if v in value_label_by_id]
            if group_by_select.value == "Category":
                value_label_by_id = {
                    c.id: c.name for c in session.query(Category).order_by(Category.name).all()
                }
            else:
                value_label_by_id = {g.id: g.name for g in list_vendor_groups(session)}
            value_select.set_options(value_label_by_id)
            value_select.value = [v for v in current if v in value_label_by_id]

        def selected_values() -> list[tuple[int | None, str]]:
            selected = list(value_select.value or [])
            if selected:
                return [(vid, value_label_by_id.get(vid, str(vid))) for vid in selected]
            label = "All Categories" if group_by_select.value == "Category" else "All Vendor Groups"
            return [(None, label)]

        def render():
            try:
                start = dt.date.fromisoformat(start_input.value)
                end = dt.date.fromisoformat(end_input.value)
            except ValueError:
                summary_label.set_text("Invalid date.")
                return
            if end < start:
                summary_label.set_text("End date is before start date.")
                table.rows = []
                table.update()
                plot.figure = go.Figure()
                plot.update()
                return

            group_by = group_by_select.value
            account_ids = list(account_select.value or []) or None
            ma_window = int(ma_input.value or 1)
            granularity = granularity_select.value

            series_by_value = [
                (
                    label,
                    expense_vs_forecast(
                        session,
                        granularity,
                        start,
                        end,
                        ma_window=ma_window,
                        category_id=value_id if group_by == "Category" else None,
                        vendor_group_id=value_id if group_by == "Vendor Group" else None,
                        account_ids=account_ids,
                    ),
                )
                for value_id, label in selected_values()
            ]

            table.rows = [
                {
                    "row_key": f"{label}-{period}",
                    "group": label,
                    "period": period,
                    "actual": _money(actual),
                    "ma": _money(ma),
                    "forecast": _money(forecast),
                }
                for label, series in series_by_value
                for period, actual, ma, forecast in zip(
                    series.bucket_labels, series.actual, series.actual_moving_avg, series.forecast
                )
            ]
            table.update()

            fig = go.Figure()
            for i, (label, series) in enumerate(series_by_value):
                color = SERIES_COLORS[i % len(SERIES_COLORS)]
                if show_actual_check.value:
                    fig.add_trace(
                        go.Scatter(
                            x=series.bucket_starts,
                            y=series.actual,
                            mode="lines",
                            name=f"{label} — Actual",
                            line=dict(color=color, width=1.5),
                        )
                    )
                if show_ma_check.value:
                    fig.add_trace(
                        go.Scatter(
                            x=series.bucket_starts,
                            y=series.actual_moving_avg,
                            mode="lines",
                            name=f"{label} — Moving Avg",
                            line=dict(color=color, width=2.5, dash="dot"),
                        )
                    )
                if show_forecast_check.value:
                    fig.add_trace(
                        go.Scatter(
                            x=series.bucket_starts,
                            y=series.forecast,
                            mode="lines",
                            name=f"{label} — Forecast",
                            line=dict(color=color, width=2, dash="dash"),
                        )
                    )
            fig.update_layout(
                title="Actual vs Forecast",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=TEXT_MUTED),
                yaxis=dict(tickprefix="£", gridcolor="#2C313C", rangemode="tozero"),
                xaxis=dict(gridcolor="#2C313C"),
                margin=dict(l=10, r=10, t=40, b=10),
                legend=dict(orientation="h", y=-0.2),
            )
            plot.figure = fig
            plot.update()

            total_actual = sum(sum(series.actual) for _, series in series_by_value)
            total_forecast = sum(sum(series.forecast) for _, series in series_by_value)
            total_matched_tx = sum(series.matched_transaction_count for _, series in series_by_value)
            total_matched_items = len(
                {i for _, series in series_by_value for i in series.matched_budget_item_ids}
            )
            summary_label.set_text(
                f"{len(series_by_value)} group(s), {total_matched_tx} matching transaction(s) — total "
                f"actual {_money(total_actual)}, total forecast {_money(total_forecast)} "
                f"({total_matched_items} matching budget item(s))."
            )

        def on_group_by_change():
            refresh_value_options()
            render()

        group_by_select.on_value_change(lambda e: on_group_by_change())
        for control in (
            value_select,
            granularity_select,
            ma_input,
            account_select,
            show_actual_check,
            show_ma_check,
            show_forecast_check,
            start_input,
            end_input,
        ):
            control.on_value_change(lambda e: render())

        refresh_value_options()
        render()
