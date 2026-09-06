import datetime as dt

import plotly.graph_objects as go
from nicegui import ui

from app.forecast import monthly_budget_summary
from app.models import Account
from app.web.layout import SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell

CATEGORY_COLORS = [
    "#3987e5",
    "#008300",
    "#d55181",
    "#c98500",
    "#199e70",
    "#d95926",
    "#9085e9",
    "#e66767",
]


def _money(value: float) -> str:
    return f"£{value:,.2f}"


@ui.page("/budget")
def budget_view_page():
    session = get_page_session()

    with page_shell("/budget", "Budget"):
        ui.label("Monthly-equivalent totals for every Budget Item active on the selected date.").style(
            f"color: {TEXT_MUTED};"
        )

        accounts = session.query(Account).order_by(Account.name).all()
        account_options = {a.id: a.name for a in accounts}

        with ui.row().classes("items-center gap-4"):
            as_of_input = ui.input("As of", value=dt.date.today().isoformat()).props("type=date")
            exclude_select = ui.select(
                account_options, multiple=True, label="Exclude accounts", value=[]
            ).classes("min-w-[260px]")
            ui.button("Refresh", on_click=lambda: render())

        with ui.row().classes("gap-6 w-full items-start"):
            with ui.column().classes("gap-2").style("flex: 3;"):
                table = ui.table(
                    columns=[
                        {"name": "flow_type", "label": "Type", "field": "flow_type", "align": "left"},
                        {"name": "category", "label": "Category", "field": "category", "align": "left"},
                        {"name": "amount", "label": "Monthly Amount", "field": "amount", "align": "right"},
                    ],
                    rows=[],
                    row_key="key",
                ).classes("w-full")

                net_label = ui.label().style("font-size: 18px; font-weight: 700;")

            with ui.column().style("flex: 2;"):
                plot = ui.plotly({}).classes("w-full")

        def render():
            as_of = dt.date.fromisoformat(as_of_input.value)
            excluded = list(exclude_select.value or [])
            summary = monthly_budget_summary(session, as_of, exclude_account_ids=excluded)

            rows = []
            total_income = 0.0
            total_expense = 0.0
            expense_rows = []
            for flow in ("Income", "Expense", "Transfer"):
                section = summary.filter(summary["flow_type"] == flow).sort("category")
                for i, row in enumerate(section.iter_rows(named=True)):
                    rows.append(
                        {
                            "key": f"{flow}-{row['category']}",
                            "flow_type": flow if i == 0 else "",
                            "category": row["category"],
                            "amount": _money(row["monthly_amount"]),
                        }
                    )
                subtotal = section["monthly_amount"].sum() if section.height else 0.0
                rows.append(
                    {
                        "key": f"{flow}-total",
                        "flow_type": "",
                        "category": f"Total {flow}",
                        "amount": _money(subtotal),
                    }
                )
                if flow == "Income":
                    total_income = subtotal
                elif flow == "Expense":
                    total_expense = subtotal
                    expense_rows = list(section.iter_rows(named=True))

            table.rows = rows
            table.update()

            net_remaining = total_income - total_expense
            net_label.set_text(f"Net Remaining (excl. transfers): {_money(net_remaining)}")
            net_label.style(
                f"color: {SUCCESS if net_remaining >= 0 else WARNING}; font-size: 18px; font-weight: 700;"
            )

            if expense_rows:
                labels = [r["category"] for r in expense_rows]
                values = [r["monthly_amount"] for r in expense_rows]
                fig = go.Figure(
                    data=[
                        go.Pie(
                            labels=labels,
                            values=values,
                            hole=0.55,
                            marker=dict(colors=CATEGORY_COLORS * (len(labels) // len(CATEGORY_COLORS) + 1)),
                        )
                    ]
                )
                fig.update_layout(
                    title="Expense Breakdown",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT_MUTED),
                    margin=dict(l=10, r=10, t=40, b=10),
                )
                plot.figure = fig
            else:
                plot.figure = go.Figure()
            plot.update()

        as_of_input.on_value_change(lambda e: render())
        exclude_select.on_value_change(lambda e: render())
        render()
