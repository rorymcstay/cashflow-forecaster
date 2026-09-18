import datetime as dt

from nicegui import ui

from app.budget_builder import aggregate_spend, vendor_options
from app.models import OCCURRENCES_PER_YEAR, Account, BudgetItem, Category, FlowType, Frequency
from app.seed import get_or_create_category
from app.transactions import query_transactions
from app.web.layout import SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell

_PREVIEW_LIMIT = 25


def _money(value: float) -> str:
    return f"£{value:,.2f}"


@ui.page("/budget-builder")
def budget_builder_page():
    session = get_page_session()

    with page_shell("/budget-builder", "Budget Builder"):
        ui.label(
            "Pick a vendor and/or category, narrow it to one or more source accounts, and see what it "
            "averages out to per period — then commit that figure as a budget line on any account. "
            "E.g. Uber on the Amex, averaged monthly; or Holiday spend across every account, averaged "
            "every 6 months and applied to one card."
        ).style(f"color: {TEXT_MUTED}; font-size: 12px;")

        accounts = session.query(Account).order_by(Account.name).all()
        account_options = {a.id: a.name for a in accounts}
        categories = session.query(Category).order_by(Category.name).all()
        category_options: dict[int | None, str] = {None: "Any category"}
        category_options.update({c.id: c.name for c in categories})

        all_transactions = query_transactions(session)
        vendors = vendor_options(all_transactions)
        vendor_select_options = {v.key: f"{v.label} ({v.transaction_count})" for v in vendors}

        with ui.row().classes("items-center gap-2 flex-wrap w-full"):
            vendor_select = (
                ui.select(
                    vendor_select_options, label="Vendors (any of)", multiple=True, value=[], with_input=True
                )
                .classes("min-w-[190px]")
                .props("use-chips")
            )
            category_select = ui.select(category_options, label="Category", value=None).classes(
                "min-w-[150px]"
            )
            source_account_select = (
                ui.select(
                    account_options,
                    label="Source accounts",
                    multiple=True,
                    value=list(account_options.keys()),
                )
                .classes("min-w-[220px]")
                .props("use-chips")
            )
            interval_select = ui.select(
                {f.value: f.value for f in Frequency},
                label="Averaging interval",
                value=Frequency.MONTHLY.value,
            ).classes("min-w-[140px]")

        with ui.row().classes("items-center gap-2 flex-wrap w-full"):
            from_input = ui.input("From (optional)").props("type=date").classes("w-40")
            to_input = ui.input("To (optional)").props("type=date").classes("w-40")
            ui.label("Leave blank to use the full history of whatever matches above.").style(
                f"color: {TEXT_MUTED}; font-size: 12px;"
            )

        with ui.row().classes("w-full gap-4 items-start flex-col md:flex-row"):
            result_container = ui.column().classes("w-full md:flex-1 gap-2")

            with ui.column().classes("w-full md:w-96 gap-2 section-card"):
                with ui.column().classes("w-full gap-2") as add_form:
                    ui.label("Add as Budget Line").classes("text-lg font-bold")
                    desc_input = ui.input("Description").classes("w-full")
                    amount_input = ui.number("Amount", value=0.0, format="%.2f").classes("w-full")
                    line_frequency_select = ui.select(
                        {f.value: f.value for f in Frequency},
                        label="Frequency",
                        value=Frequency.MONTHLY.value,
                    ).classes("w-full")
                    flow_select = ui.select(
                        {ft.value: ft.value for ft in FlowType},
                        label="Type",
                        value=FlowType.EXPENSE.value,
                    ).classes("w-full")
                    budget_category_input = ui.select(
                        sorted({c.name for c in categories}),
                        label="Category",
                        with_input=True,
                        new_value_mode="add-unique",
                    ).classes("w-full")
                    target_account_select = ui.select(
                        account_options, label="Target account", value=None
                    ).classes("w-full")
                    from_date_input = (
                        ui.input("Effective From", value=dt.date.today().isoformat())
                        .props("type=date")
                        .classes("w-full")
                    )
                    ui.button("+ Add Budget Line", on_click=lambda: add_budget_line()).classes("w-full")
                add_form.set_visibility(False)

                ui.separator()
                ui.label("All Budget Lines").classes("text-lg font-bold")
                all_lines_table = ui.table(
                    columns=[
                        {"name": "status", "label": "", "field": "status", "align": "left"},
                        {
                            "name": "description",
                            "label": "Description",
                            "field": "description",
                            "align": "left",
                        },
                        {"name": "category", "label": "Category", "field": "category", "align": "left"},
                        {"name": "amount", "label": "Amount", "field": "amount", "align": "right"},
                        {
                            "name": "frequency",
                            "label": "Frequency",
                            "field": "frequency",
                            "align": "left",
                        },
                        {"name": "account", "label": "Account", "field": "account", "align": "left"},
                    ],
                    rows=[],
                    row_key="id",
                    pagination=8,
                ).classes("w-full")
                all_lines_table.add_slot(
                    "body-cell-status",
                    """
                    <q-td :props="props">
                        <q-badge v-if="props.value" color="primary" :label="props.value" />
                    </q-td>
                    """,
                )

        current_aggregate = {"value": None}
        added_item_ids: set[int] = set()
        line_frequency_state = {"value": Frequency.MONTHLY.value}

        def _stat(title: str, value: str, color: str | None = None) -> None:
            with ui.column().classes("stat-card"):
                ui.label(title).style(f"color: {TEXT_MUTED}; font-size: 11px;")
                ui.label(value).style(
                    f"font-size: 20px; font-weight: 700;{f' color: {color};' if color else ''}"
                )

        def recompute():
            result_container.clear()
            vendor_keys = vendor_select.value or None
            category_id = category_select.value
            interval = Frequency(interval_select.value)

            if not vendor_keys and category_id is None:
                add_form.set_visibility(False)
                current_aggregate["value"] = None
                with result_container:
                    ui.label("Choose a vendor and/or category to aggregate.").style(f"color: {TEXT_MUTED};")
                return

            start_date = dt.date.fromisoformat(from_input.value) if from_input.value else None
            end_date = dt.date.fromisoformat(to_input.value) if to_input.value else None
            source_account_ids = source_account_select.value or None

            base = query_transactions(
                session,
                account_ids=source_account_ids,
                category_ids=[category_id] if category_id is not None else None,
                start_date=start_date,
                end_date=end_date,
            )
            aggregate = aggregate_spend(
                base, interval, vendor_keys=vendor_keys, start_date=start_date, end_date=end_date
            )
            current_aggregate["value"] = aggregate

            with result_container:
                if not aggregate.transactions:
                    ui.label("No matching transactions for this combination.").style(f"color: {TEXT_MUTED};")
                    add_form.set_visibility(False)
                    return

                with ui.row().classes("gap-4 w-full flex-wrap"):
                    _stat("Matched transactions", str(len(aggregate.transactions)))
                    _stat(
                        "Window",
                        f"{aggregate.start_date:%d %b %Y} – {aggregate.end_date:%d %b %Y}"
                        if aggregate.start_date and aggregate.end_date
                        else "—",
                    )
                    _stat("Total", _money(aggregate.total), WARNING if aggregate.total < 0 else SUCCESS)
                    _stat(
                        f"Average per {interval_select.value}",
                        _money(aggregate.average_per_period),
                        WARNING if aggregate.average_per_period < 0 else SUCCESS,
                    )

                ui.label(f"Matching transactions ({len(aggregate.transactions)})").classes("font-bold mt-2")
                for t in aggregate.transactions[:_PREVIEW_LIMIT]:
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(t.date.strftime("%d %b %Y")).style("width: 100px;")
                        ui.label(t.statement.account.name).style(f"width: 140px; color: {TEXT_MUTED};")
                        ui.label(t.description).style("flex: 1;")
                        ui.label(_money(t.amount)).style(f"color: {WARNING if t.amount < 0 else SUCCESS};")
                if len(aggregate.transactions) > _PREVIEW_LIMIT:
                    ui.label(f"... and {len(aggregate.transactions) - _PREVIEW_LIMIT} more.").style(
                        f"color: {TEXT_MUTED};"
                    )

            # Prefill the add-line form from this aggregate — a fresh group
            # each time you recompute, so overwriting is the expected result.
            vendor_labels = [v.label for v in vendors if v.key in (vendor_keys or [])]
            vendor_label = " + ".join(vendor_labels) if vendor_labels else None
            category_label = category_options.get(category_id) if category_id is not None else None
            desc_input.value = vendor_label or category_label or ""
            amount_input.value = abs(aggregate.average_per_period)
            # The committed budget line's frequency defaults to whatever
            # interval was just used to average history, but is
            # independently editable below (on_line_frequency_changed
            # rescales the amount rather than re-running the whole
            # aggregate) — e.g. average annual holiday spend but commit it
            # as a smoothed monthly line.
            line_frequency_select.value = interval_select.value
            line_frequency_state["value"] = interval_select.value
            flow_select.value = (
                FlowType.INCOME.value if aggregate.average_per_period > 0 else FlowType.EXPENSE.value
            )
            if category_label:
                budget_category_input.value = category_label
            elif aggregate.transactions and aggregate.transactions[0].category:
                budget_category_input.value = aggregate.transactions[0].category.name
            if source_account_ids and len(source_account_ids) == 1:
                target_account_select.value = source_account_ids[0]
            add_form.set_visibility(True)

        def on_line_frequency_changed():
            new_freq = Frequency(line_frequency_select.value)
            old_freq = Frequency(line_frequency_state["value"])
            if new_freq != old_freq and amount_input.value:
                rescaled = (
                    amount_input.value * OCCURRENCES_PER_YEAR[old_freq] / OCCURRENCES_PER_YEAR[new_freq]
                )
                amount_input.value = round(rescaled, 2)
            line_frequency_state["value"] = line_frequency_select.value

        def refresh_budget_lines():
            items = session.query(BudgetItem).all()
            all_lines_table.rows = [
                {
                    "id": item.id,
                    "status": "New" if item.id in added_item_ids else "",
                    "description": item.description,
                    "category": item.category.name if item.category else "—",
                    "amount": _money(item.amount),
                    "frequency": item.frequency.value,
                    "account": item.account.name,
                }
                for item in sorted(
                    items, key=lambda i: (i.id not in added_item_ids, i.account.name, i.description)
                )
            ]
            all_lines_table.update()

        def add_budget_line():
            aggregate = current_aggregate["value"]
            if aggregate is None or not aggregate.transactions:
                ui.notify("Nothing to add — adjust the filters above first.", type="negative")
                return
            description = desc_input.value.strip()
            if not description:
                ui.notify("Please enter a description.", type="negative")
                return
            category_name = (budget_category_input.value or "").strip()
            if not category_name:
                ui.notify("Please enter or choose a category.", type="negative")
                return
            if target_account_select.value is None:
                ui.notify("Choose a target account.", type="negative")
                return

            line_frequency = Frequency(line_frequency_select.value)
            item = BudgetItem(
                description=description,
                amount=abs(amount_input.value),
                flow_type=FlowType(flow_select.value),
                frequency=line_frequency,
                effective_from=dt.date.fromisoformat(from_date_input.value),
                category=get_or_create_category(session, category_name),
                account_id=target_account_select.value,
            )
            session.add(item)
            session.commit()
            added_item_ids.add(item.id)
            refresh_budget_lines()
            ui.notify(f"Added “{description}” as a budget line.", type="positive")

        vendor_select.on_value_change(lambda e: recompute())
        category_select.on_value_change(lambda e: recompute())
        source_account_select.on_value_change(lambda e: recompute())
        interval_select.on_value_change(lambda e: recompute())
        line_frequency_select.on_value_change(lambda e: on_line_frequency_changed())
        from_input.on_value_change(lambda e: recompute())
        to_input.on_value_change(lambda e: recompute())

        refresh_budget_lines()
