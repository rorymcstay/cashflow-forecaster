"""Cross-account Transactions tab: an ag-Grid table with per-column filters,
a group-by toggle (subtotalled, since row-grouping itself is an AG Grid
Enterprise feature we don't have), multi-row selection with a running sum of
the selected rows, and a detail pane to the right of the table for
reclassifying a transaction and exploring similar/recurring ones by merchant.
"""

import datetime as dt
import itertools

import plotly.graph_objects as go
from nicegui import ui

from app.models import Account, BudgetItem, Category, FlowType, Frequency, Transaction
from app.seed import get_or_create_category
from app.statement_import import UNCATEGORIZED, generate_suggestions, match_budget_item
from app.transactions import (
    DATE_RANGE_PRESETS,
    UNCATEGORIZED_ID,
    date_range_preset,
    merchant_key,
    query_transactions,
    recurring_groups_by_merchant,
    similar_transactions,
    spend_breakdown,
)
from app.web.layout import BORDER, SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell

_RANGE_OPTIONS = {k: v for k, v in DATE_RANGE_PRESETS.items() if k != "custom"}
_CATEGORY_COLORS = ["#5B8DEF", "#34D399", "#F2555C", "#F2B705", "#B45BEF", "#05C7F2", "#F2905B", "#8D95A3"]

GROUP_FIELDS = {
    "None": None,
    "Category": "category",
    "Account": "account",
    "Merchant": "merchant",
    "Month": "month",
}

_DATE_VALUE_FORMATTER = (
    "(params) => { if (!params.value) return ''; "
    "const [y, m, d] = params.value.split('-').map(Number); "
    "return new Date(y, m - 1, d).toLocaleDateString('en-GB', "
    "{day: '2-digit', month: 'short', year: 'numeric'}); }"
)
_DATE_FILTER_COMPARATOR = (
    "(filterLocalDateAtMidnight, cellValue) => { if (!cellValue) return -1; "
    "const [y, m, d] = cellValue.split('-').map(Number); "
    "const cellDate = new Date(y, m - 1, d); "
    "if (cellDate < filterLocalDateAtMidnight) return -1; "
    "if (cellDate > filterLocalDateAtMidnight) return 1; return 0; }"
)
_AMOUNT_VALUE_FORMATTER = (
    "(params) => { if (params.value === null || params.value === undefined) return ''; "
    "const v = params.value; const sign = v < 0 ? '-' : ''; "
    "return sign + '£' + Math.abs(v).toLocaleString('en-GB', "
    "{minimumFractionDigits: 2, maximumFractionDigits: 2}); }"
)
_AMOUNT_CELL_STYLE = f"(params) => params.value < 0 ? {{color: '{WARNING}'}} : {{color: '{SUCCESS}'}}"
_GROUP_ROW_STYLE = "(params) => params.data && params.data._group_header ? {fontWeight: 'bold'} : undefined"
_SELECTABLE = "(params) => !(params.data && params.data._group_header)"


def _money(value: float | None) -> str:
    return "" if value is None else f"£{value:,.2f}"


def _stat_card(title: str) -> ui.label:
    with ui.column().classes("stat-card"):
        ui.label(title).style(f"color: {TEXT_MUTED}; font-size: 11px;")
        value_label = ui.label("—").style("font-size: 22px; font-weight: 700;")
    return value_label


def _row_dict(t: Transaction, recurring_groups: dict[str, dict]) -> dict:
    key = merchant_key(t)
    group = recurring_groups.get(key)
    return {
        "id": t.id,
        "date": t.date.isoformat(),
        "month": t.date.strftime("%Y-%m"),
        "month_label": t.date.strftime("%B %Y"),
        "account": t.statement.account.name,
        "description": t.description,
        "category": t.category.name if t.category else UNCATEGORIZED,
        "amount": t.amount,
        "merchant": key,
        "recurring": group["guessed_frequency"] if group else "",
    }


def _group_label(field: str, key: str, group_rows: list[dict]) -> str:
    if field == "month":
        return group_rows[0]["month_label"]
    return key or "—"


def _build_grid_rows(rows: list[dict], group_by_label: str) -> list[dict]:
    field = GROUP_FIELDS[group_by_label]
    if field is None:
        return sorted(rows, key=lambda r: r["date"], reverse=True)

    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    rows.sort(key=lambda r: r[field])  # stable: preserves the date-desc order within each group

    grid_rows = []
    for key, group_iter in itertools.groupby(rows, key=lambda r: r[field]):
        group_rows = list(group_iter)
        total = sum(r["amount"] for r in group_rows)
        label = _group_label(field, key, group_rows)
        grid_rows.append(
            {
                "id": f"group::{field}::{key}",
                "date": "",
                "description": f"{label}  —  {len(group_rows)} · {_money(total)}",
                "account": "",
                "category": "",
                "amount": None,
                "recurring": "",
                "_group_header": True,
            }
        )
        grid_rows.extend(group_rows)
    return grid_rows


@ui.page("/transactions")
def transactions_page():
    session = get_page_session()

    with page_shell("/transactions", "Transactions"):
        accounts = session.query(Account).order_by(Account.name).all()
        account_options = {a.id: a.name for a in accounts}

        categories = session.query(Category).order_by(Category.name).all()
        category_options = {c.id: c.name for c in categories}
        category_options[UNCATEGORIZED_ID] = "Uncategorized"

        all_transactions = query_transactions(session)
        recurring_groups = recurring_groups_by_merchant(all_transactions)

        with ui.row().classes("items-center gap-4 flex-wrap w-full"):
            account_select = (
                ui.select(
                    account_options, label="Accounts", multiple=True, value=list(account_options.keys())
                )
                .classes("min-w-[220px]")
                .props("use-chips")
            )
            category_select = (
                ui.select(
                    category_options, label="Categories", multiple=True, value=list(category_options.keys())
                )
                .classes("min-w-[220px]")
                .props("use-chips")
            )
            group_select = ui.select(list(GROUP_FIELDS), label="Group by", value="None")
            search_input = ui.input("Search").props("debounce=300 clearable")

        with ui.row().classes("items-center gap-4 flex-wrap w-full"):
            range_select = ui.select(_RANGE_OPTIONS, label="Date range", value="all").classes("min-w-[160px]")
            from_input = ui.input("From").props("type=date").classes("w-40")
            to_input = ui.input("To").props("type=date").classes("w-40")

        with ui.tabs().classes("w-full") as page_tabs:
            table_tab = ui.tab("Table")
            summary_tab = ui.tab("Summary")

        with ui.tab_panels(page_tabs, value=table_tab).classes("w-full"):
            with ui.tab_panel(table_tab), ui.row().classes("w-full gap-4 items-start"):
                with ui.column().classes("flex-1 gap-1"):
                    grid = (
                        ui.aggrid(
                            {
                                "columnDefs": [
                                    {
                                        "field": "_select",
                                        "headerName": "",
                                        "checkboxSelection": True,
                                        ":checkboxSelection": _SELECTABLE,
                                        "headerCheckboxSelection": True,
                                        "width": 44,
                                        "pinned": "left",
                                        "filter": False,
                                        "sortable": False,
                                        "resizable": False,
                                    },
                                    {
                                        "field": "date",
                                        "headerName": "Date",
                                        "filter": "agDateColumnFilter",
                                        "filterParams": {
                                            "browserDatePicker": True,
                                            ":comparator": _DATE_FILTER_COMPARATOR,
                                        },
                                        "floatingFilter": True,
                                        ":valueFormatter": _DATE_VALUE_FORMATTER,
                                        "sort": "desc",
                                        "width": 120,
                                    },
                                    {
                                        "field": "account",
                                        "headerName": "Account",
                                        "filter": "agTextColumnFilter",
                                        "floatingFilter": True,
                                        "width": 140,
                                    },
                                    {
                                        "field": "description",
                                        "headerName": "Description",
                                        "filter": "agTextColumnFilter",
                                        "floatingFilter": True,
                                        "flex": 1,
                                        "minWidth": 220,
                                    },
                                    {
                                        "field": "category",
                                        "headerName": "Category",
                                        "filter": "agTextColumnFilter",
                                        "floatingFilter": True,
                                        "width": 140,
                                    },
                                    {
                                        "field": "recurring",
                                        "headerName": "Recurring",
                                        "filter": "agTextColumnFilter",
                                        "floatingFilter": True,
                                        "width": 110,
                                    },
                                    {
                                        "field": "amount",
                                        "headerName": "Amount",
                                        "filter": "agNumberColumnFilter",
                                        "floatingFilter": True,
                                        ":valueFormatter": _AMOUNT_VALUE_FORMATTER,
                                        ":cellStyle": _AMOUNT_CELL_STYLE,
                                        "type": "rightAligned",
                                        "width": 130,
                                    },
                                ],
                                "rowData": [],
                                "rowSelection": "multiple",
                                "suppressRowClickSelection": True,
                                ":getRowId": "(params) => String(params.data.id)",
                                ":getRowStyle": _GROUP_ROW_STYLE,
                                "animateRows": False,
                            },
                            auto_size_columns=False,
                        )
                        .classes("w-full")
                        .style("height: 65vh;")
                    )
                    selection_label = ui.label("No rows selected").style(f"color: {TEXT_MUTED};")

                with ui.column().classes("w-96 gap-2 section-card"):
                    detail_container = ui.column().classes("w-full gap-2")
                    with detail_container:
                        ui.label("Click a transaction to see details here.").style(f"color: {TEXT_MUTED};")

            with ui.tab_panel(summary_tab):
                with ui.row().classes("gap-4 w-full flex-wrap"):
                    income_stat = _stat_card("Total Income")
                    expense_stat = _stat_card("Total Expenses")
                    net_stat = _stat_card("Net")
                    avg_expense_stat = _stat_card("Avg Monthly Spend")
                    count_stat = _stat_card("Transactions")

                with ui.row().classes("w-full gap-4 items-start flex-wrap"):
                    with ui.column().classes("flex-1 min-w-[360px] gap-1"):
                        ui.label("Spend by Category").classes("text-lg font-bold")
                        category_plot = ui.plotly({}).classes("w-full").style("height: 320px;")
                    with ui.column().classes("flex-1 min-w-[320px] gap-1"):
                        ui.label("Top Merchants").classes("text-lg font-bold")
                        merchants_table = ui.table(
                            columns=[
                                {
                                    "name": "merchant",
                                    "label": "Merchant",
                                    "field": "merchant",
                                    "align": "left",
                                },
                                {"name": "count", "label": "Count", "field": "count", "align": "right"},
                                {"name": "amount", "label": "Total", "field": "amount", "align": "right"},
                            ],
                            rows=[],
                            row_key="merchant",
                            pagination=10,
                        ).classes("w-full")

                ui.label("Income vs Expense by Month").classes("text-lg font-bold")
                monthly_plot = ui.plotly({}).classes("w-full").style("height: 320px;")

        def reload_rows():
            selected_account_ids = account_select.value or None
            selected_category_ids = category_select.value or None
            start_date = dt.date.fromisoformat(from_input.value) if from_input.value else None
            end_date = dt.date.fromisoformat(to_input.value) if to_input.value else None
            displayed = query_transactions(
                session,
                account_ids=selected_account_ids,
                category_ids=selected_category_ids,
                start_date=start_date,
                end_date=end_date,
            )
            rows = [_row_dict(t, recurring_groups) for t in displayed]
            grid.options["rowData"] = _build_grid_rows(rows, group_select.value)
            grid.update()
            selection_label.set_text("No rows selected")
            refresh_summary(displayed)

        def refresh_summary(displayed: list[Transaction]):
            breakdown = spend_breakdown(displayed)
            income_stat.set_text(_money(breakdown["total_income"]))
            expense_stat.set_text(_money(breakdown["total_expense"]))
            net_stat.set_text(_money(breakdown["net"]))
            net_stat.style(
                f"color: {SUCCESS if breakdown['net'] >= 0 else WARNING}; font-size: 22px; font-weight: 700;"
            )
            avg_expense_stat.set_text(_money(breakdown["avg_monthly_expense"]))
            count_stat.set_text(str(breakdown["transaction_count"]))

            top_categories = list(reversed(breakdown["by_category"][:12]))
            n = len(top_categories)
            bar_colors = [_CATEGORY_COLORS[(n - 1 - i) % len(_CATEGORY_COLORS)] for i in range(n)]
            cat_fig = go.Figure()
            cat_fig.add_trace(
                go.Bar(
                    x=[c["amount"] for c in top_categories],
                    y=[c["category"] for c in top_categories],
                    orientation="h",
                    marker_color=bar_colors,
                    text=[f"{c['pct']:.0f}%" for c in top_categories],
                    textposition="outside",
                )
            )
            cat_fig.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=TEXT_MUTED),
                xaxis=dict(tickprefix="£", gridcolor=BORDER),
                yaxis=dict(gridcolor=BORDER),
                margin=dict(l=10, r=10, t=10, b=10),
                showlegend=False,
            )
            category_plot.figure = cat_fig
            category_plot.update()

            merchants_table.rows = [
                {"merchant": m["merchant"], "count": m["count"], "amount": _money(m["amount"])}
                for m in breakdown["top_merchants"]
            ]
            merchants_table.update()

            months = breakdown["by_month"]
            month_fig = go.Figure()
            month_fig.add_trace(
                go.Bar(
                    x=[m["month_label"] for m in months],
                    y=[m["income"] for m in months],
                    name="Income",
                    marker_color=SUCCESS,
                )
            )
            month_fig.add_trace(
                go.Bar(
                    x=[m["month_label"] for m in months],
                    y=[m["expense"] for m in months],
                    name="Expense",
                    marker_color=WARNING,
                )
            )
            month_fig.update_layout(
                barmode="group",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=TEXT_MUTED),
                yaxis=dict(tickprefix="£", gridcolor=BORDER),
                xaxis=dict(gridcolor=BORDER),
                margin=dict(l=10, r=10, t=30, b=10),
                legend=dict(orientation="h", y=1.15),
            )
            monthly_plot.figure = month_fig
            monthly_plot.update()

        def apply_range_preset():
            start, end = date_range_preset(range_select.value)
            from_input.value = start.isoformat() if start else None
            to_input.value = end.isoformat() if end else None
            reload_rows()

        async def handle_selection_changed():
            selected = await grid.get_selected_rows()
            real_rows = [r for r in selected if not r.get("_group_header")]
            if not real_rows:
                selection_label.set_text("No rows selected")
                return
            total = sum(r.get("amount") or 0 for r in real_rows)
            selection_label.set_text(f"{len(real_rows)} selected · Sum {_money(total)}")

        def handle_cell_clicked(e):
            data = e.args.get("data") or {}
            if data.get("_group_header"):
                return
            transaction = session.get(Transaction, data.get("id"))
            if transaction is not None:
                show_detail(transaction)

        def open_budget_item_dialog(transaction: Transaction):
            account_options = {a.id: a.name for a in session.query(Account).order_by(Account.name).all()}
            categories = sorted({c.name for c in session.query(Category).all()})

            with ui.dialog() as dialog, ui.card().classes("gap-2 min-w-[420px]"):
                ui.label("Add Budget Item").classes("text-lg font-bold")
                desc_input = ui.input("Description", value=transaction.description)
                amount_input = ui.number("Amount", value=abs(transaction.amount), format="%.2f")
                flow_select = ui.select(
                    {ft.value: ft.value for ft in FlowType},
                    label="Type",
                    value=(FlowType.INCOME.value if transaction.amount > 0 else FlowType.EXPENSE.value),
                )
                freq_select = ui.select(
                    {f.value: f.value for f in Frequency}, label="Frequency", value=Frequency.MONTHLY.value
                )
                from_input = ui.input("Effective From", value=dt.date.today().isoformat()).props("type=date")
                category_input = ui.select(
                    categories,
                    label="Category",
                    value=(transaction.category.name if transaction.category else None),
                    with_input=True,
                    new_value_mode="add-unique",
                )
                account_select = ui.select(
                    account_options, label="Account", value=transaction.statement.account_id
                )

                def save():
                    description = desc_input.value.strip()
                    if not description:
                        ui.notify("Please enter a description.", type="negative")
                        return
                    category_name = (category_input.value or "").strip()
                    if not category_name:
                        ui.notify("Please enter or choose a category.", type="negative")
                        return
                    if account_select.value is None:
                        ui.notify("Choose an account.", type="negative")
                        return

                    item = BudgetItem(
                        description=description,
                        amount=amount_input.value,
                        flow_type=FlowType(flow_select.value),
                        frequency=Frequency(freq_select.value),
                        effective_from=dt.date.fromisoformat(from_input.value),
                        category=get_or_create_category(session, category_name),
                        account_id=account_select.value,
                    )
                    session.add(item)
                    session.commit()
                    dialog.close()
                    ui.notify(f"Added budget item “{description}”.", type="positive")
                    show_detail(transaction)

                with ui.row().classes("justify-end w-full gap-2 mt-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", on_click=save)

            dialog.open()

        def show_detail(transaction: Transaction):
            detail_container.clear()
            key = merchant_key(transaction)
            group = recurring_groups.get(key)
            matches = similar_transactions(transaction, all_transactions)
            categories = sorted({c.name for c in session.query(Category).all()})

            with detail_container:
                ui.label(transaction.date.strftime("%d %b %Y")).classes("text-lg font-bold")
                ui.label(transaction.description)
                ui.label(_money(transaction.amount)).style(
                    f"color: {WARNING if transaction.amount < 0 else SUCCESS}; font-weight: bold;"
                )
                ui.label(transaction.statement.account.name).style(f"color: {TEXT_MUTED};")

                ui.separator()
                ui.label("Reclassify").classes("font-bold")
                category_input = ui.select(
                    categories,
                    value=(transaction.category.name if transaction.category else None),
                    with_input=True,
                    new_value_mode="add-unique",
                ).classes("w-full")

                def save_category():
                    name = (category_input.value or "").strip()
                    if not name:
                        ui.notify("Please enter or choose a category.", type="negative")
                        return
                    transaction.category = get_or_create_category(session, name)
                    session.commit()
                    ui.notify(f"Reclassified as {name}.", type="positive")
                    reload_rows()
                    show_detail(transaction)

                ui.button("Save category", on_click=save_category).classes("w-full")

                budget_item = transaction.matched_budget_item
                if budget_item is None:
                    budget_item = match_budget_item(
                        session, transaction.statement.account_id, transaction.description, transaction.date
                    )
                ui.label(f"Budget item: {budget_item.description if budget_item else '—'}").style(
                    f"color: {TEXT_MUTED};"
                )
                ui.button("+ Add Budget Item", on_click=lambda: open_budget_item_dialog(transaction)).props(
                    "flat dense"
                )

                ui.separator()
                ui.label("Recurring").classes("font-bold")
                if group is None:
                    ui.label("No recurring pattern detected for this merchant yet.").style(
                        f"color: {TEXT_MUTED};"
                    )
                else:
                    ui.label(
                        f"{group['guessed_frequency']} · seen {group['occurrences']}x "
                        f"({group['first_date']} – {group['last_date']}), avg {_money(group['avg_amount'])}"
                    )
                    ui.button(
                        "Refresh budget suggestions from this",
                        on_click=lambda: (
                            generate_suggestions(session, transaction.statement.account),
                            ui.notify("Suggestions refreshed — see the Statements page.", type="positive"),
                        ),
                    ).props("flat dense")

                ui.separator()
                ui.label(f"Similar transactions ({len(matches)})").classes("font-bold")
                if not matches:
                    ui.label("No other transactions match this merchant.").style(f"color: {TEXT_MUTED};")
                for m in matches:
                    with (
                        ui.row()
                        .classes("w-full items-center gap-2 cursor-pointer")
                        .on("click", lambda _e, m=m: show_detail(m))
                    ):
                        ui.label(m.date.strftime("%d %b %Y")).style("width: 90px;")
                        ui.label(m.statement.account.name).style(f"width: 110px; color: {TEXT_MUTED};")
                        ui.label(_money(m.amount)).style(f"color: {WARNING if m.amount < 0 else SUCCESS};")

        grid.on("selectionChanged", handle_selection_changed)
        grid.on("cellClicked", handle_cell_clicked)
        account_select.on_value_change(lambda e: reload_rows())
        category_select.on_value_change(lambda e: reload_rows())
        group_select.on_value_change(lambda e: reload_rows())
        search_input.on_value_change(
            lambda e: grid.run_grid_method("setGridOption", "quickFilterText", e.value)
        )
        range_select.on_value_change(lambda e: apply_range_preset())
        from_input.on_value_change(lambda e: reload_rows())
        to_input.on_value_change(lambda e: reload_rows())

        reload_rows()
