"""Cross-account Transactions tab: an ag-Grid table with per-column filters,
a group-by toggle (subtotalled, since row-grouping itself is an AG Grid
Enterprise feature we don't have), multi-row selection with a running sum of
the selected rows, and a detail pane to the right of the table for
reclassifying a transaction and exploring similar/recurring ones by merchant.
"""

import itertools

from nicegui import ui

from app.models import Account, Category, Transaction
from app.seed import get_or_create_category
from app.statement_import import UNCATEGORIZED, generate_suggestions, match_budget_item
from app.transactions import (
    merchant_key,
    query_transactions,
    recurring_groups_by_merchant,
    similar_transactions,
)
from app.web.layout import SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell

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
            group_select = ui.select(list(GROUP_FIELDS), label="Group by", value="None")
            search_input = ui.input("Search").props("debounce=300 clearable")

        with ui.row().classes("w-full gap-4 items-start"):
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

        def reload_rows():
            selected_account_ids = account_select.value or None
            displayed = query_transactions(session, account_ids=selected_account_ids)
            rows = [_row_dict(t, recurring_groups) for t in displayed]
            grid.options["rowData"] = _build_grid_rows(rows, group_select.value)
            grid.update()
            selection_label.set_text("No rows selected")

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
        group_select.on_value_change(lambda e: reload_rows())
        search_input.on_value_change(
            lambda e: grid.run_grid_method("setGridOption", "quickFilterText", e.value)
        )

        reload_rows()
