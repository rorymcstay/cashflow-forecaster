import datetime as dt

from nicegui import ui

from app.budget_builder import (
    StagedLine,
    aggregate_spend,
    commit_staged_line,
    uncaptured_by_vendor,
    uncaptured_transactions,
    vendor_options,
)
from app.budget_recommender import recommend_budget
from app.budgets import active_budget_items
from app.models import OCCURRENCES_PER_YEAR, Account, Category, FlowType, Frequency
from app.transactions import query_transactions
from app.web.layout import SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell

_PREVIEW_LIMIT = 25
_UNCAPTURED_LIMIT = 30


def _money(value: float) -> str:
    return f"£{value:,.2f}"


@ui.page("/budget-builder")
def budget_builder_page():
    session = get_page_session()

    with page_shell("/budget-builder", "Budget Builder"):
        ui.label(
            "Pick a vendor and/or category, narrow it to one or more source accounts, and see what it "
            "averages out to per period — then stage it as a budget line on any account. Stage as many "
            "lines as you like and save them together. E.g. Uber on the Amex, averaged monthly; or "
            "Holiday spend across every account, averaged every 6 months and applied to one card."
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
            ui.label("Global date range").classes("font-bold").style("align-self: center;")
            ui.label("(used to compute every line below):").style(f"color: {TEXT_MUTED};")
            from_input = ui.input("From (optional)").props("type=date").classes("w-40")
            to_input = ui.input("To (optional)").props("type=date").classes("w-40")
            ui.label("Leave blank to use the full history of whatever matches above.").style(
                f"color: {TEXT_MUTED}; font-size: 12px;"
            )

        with ui.row().classes("items-center gap-2"):
            ui.button("🔮 Recommend Budget", on_click=lambda: recommend_clicked())
            ui.label(
                "Proposes a full slate of staged lines from transaction history — recurring bills, "
                "usage-based charges, and a category catch-all for the rest. Only the Source accounts "
                "filter above applies to it."
            ).style(f"color: {TEXT_MUTED}; font-size: 12px;")

        with ui.row().classes("w-full gap-4 items-start flex-col md:flex-row"):
            result_container = ui.column().classes("w-full md:flex-1 gap-2")

            with ui.column().classes("w-full md:w-[420px] gap-2 section-card"):
                with ui.column().classes("w-full gap-2") as add_form:
                    ui.label("Stage as Budget Line").classes("text-lg font-bold")
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
                    vendor_scope_label = ui.label("").style(f"color: {TEXT_MUTED}; font-size: 12px;")
                    ui.button("+ Stage Budget Line", on_click=lambda: stage_line()).classes("w-full")
                add_form.set_visibility(False)

                ui.separator()
                ui.label("Staged Lines (not yet saved)").classes("text-lg font-bold")
                staged_container = ui.column().classes("w-full gap-1")
                save_all_button = ui.button(
                    "Save All Staged Lines", on_click=lambda: save_all_staged()
                ).classes("w-full")
                save_all_button.set_visibility(False)

                ui.separator()
                ui.label("Existing Budget Items").classes("text-lg font-bold")
                all_lines_table = ui.table(
                    columns=[
                        {"name": "status", "label": "", "field": "status", "align": "left"},
                        {
                            "name": "description",
                            "label": "Description",
                            "field": "description",
                            "align": "left",
                        },
                        {"name": "vendors", "label": "Vendors", "field": "vendors", "align": "left"},
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
        saved_item_ids: set[int] = set()
        line_frequency_state = {"value": Frequency.MONTHLY.value}
        staged_lines: list[StagedLine] = []

        def _stat(title: str, value: str, color: str | None = None) -> None:
            with ui.column().classes("stat-card"):
                ui.label(title).style(f"color: {TEXT_MUTED}; font-size: 11px;")
                ui.label(value).style(
                    f"font-size: 20px; font-weight: 700;{f' color: {color};' if color else ''}"
                )

        def render_uncaptured(base: list) -> None:
            """Below the matching-transactions preview: whichever of `base`
            (the same account/category/date scope, but *not* narrowed by the
            vendor filter) has no covering budget item yet — ranked by
            spend, so it doubles as a shortlist of what to build a line for
            next. Click a row to filter the vendor picker above to it."""
            rows = uncaptured_by_vendor(uncaptured_transactions(session, base))
            ui.label(f"Uncaptured spend in this scope ({len(rows)} vendors)").classes("font-bold mt-2")
            ui.label("Click a row to filter the vendor picker above to it.").style(
                f"color: {TEXT_MUTED}; font-size: 11px;"
            )

            def pick_uncaptured_vendor(_e):
                if not uncap_table.selected:
                    return
                key = uncap_table.selected[0]["key"]
                uncap_table.selected = []
                vendor_select.value = [key]
                recompute()

            uncap_table = ui.table(
                columns=[
                    {"name": "vendor", "label": "Vendor", "field": "vendor", "align": "left"},
                    {"name": "count", "label": "Count", "field": "count", "align": "right"},
                    {"name": "total", "label": "Total", "field": "total", "align": "right"},
                ],
                rows=[
                    {"key": r.key, "vendor": r.label, "count": r.transaction_count, "total": _money(r.total)}
                    for r in rows[:_UNCAPTURED_LIMIT]
                ],
                row_key="key",
                selection="single",
                pagination=10,
                on_select=pick_uncaptured_vendor,
            ).classes("w-full")

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
                    render_uncaptured(base)
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

                render_uncaptured(base)

            # Prefill the stage form from this aggregate — a fresh group
            # each time you recompute, so overwriting is the expected result.
            vendor_labels = [v.label for v in vendors if v.key in (vendor_keys or [])]
            vendor_label = " + ".join(vendor_labels) if vendor_labels else None
            category_label = category_options.get(category_id) if category_id is not None else None
            desc_input.value = vendor_label or category_label or ""
            amount_input.value = abs(aggregate.average_per_period)
            # The staged line's frequency defaults to whatever interval was
            # just used to average history, but is independently editable
            # below (on_line_frequency_changed rescales the amount rather
            # than re-running the whole aggregate) — e.g. average annual
            # holiday spend but stage it as a smoothed monthly line.
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

            if vendor_keys and category_id is not None:
                vendor_scope_label.set_text(
                    f"Scoped to {len(vendor_keys)} vendor(s) — other {category_label} transactions stay "
                    "uncaptured."
                )
            elif vendor_keys:
                vendor_scope_label.set_text(f"Scoped to {len(vendor_keys)} vendor(s), any category.")
            else:
                vendor_scope_label.set_text(f"Whole category ({category_label}) — every vendor in it.")
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
            items = active_budget_items(session).all()
            all_lines_table.rows = [
                {
                    "id": item.id,
                    "status": "New" if item.id in saved_item_ids else "",
                    "description": item.description,
                    "vendors": ", ".join(item.vendor_list) or "—",
                    "category": item.category.name if item.category else "—",
                    "amount": _money(item.amount),
                    "frequency": item.frequency.value,
                    "account": item.account.name,
                }
                for item in sorted(
                    items, key=lambda i: (i.id not in saved_item_ids, i.account.name, i.description)
                )
            ]
            all_lines_table.update()

        def render_staged():
            staged_container.clear()
            save_all_button.set_visibility(bool(staged_lines))
            save_all_button.set_text(
                f"Save All Staged Lines ({len(staged_lines)})" if staged_lines else "Save All Staged Lines"
            )
            with staged_container:
                for i, staged in enumerate(staged_lines):
                    with (
                        ui.row()
                        .classes("w-full items-center gap-2")
                        .style(f"border: 1px solid {TEXT_MUTED}; border-radius: 6px; padding: 4px 8px;")
                    ):
                        with ui.column().classes("gap-0").style("flex: 1;"):
                            ui.label(("🔮 " if staged.rationale else "") + staged.description).style(
                                "font-weight: 600;"
                            )
                            ui.label(
                                f"{staged.vendor_label or 'Whole category'} · {staged.category_name} · "
                                f"{_money(staged.amount)} {staged.frequency.value} · "
                                f"{account_options.get(staged.account_id, '—')}"
                            ).style(f"color: {TEXT_MUTED}; font-size: 12px;")
                            window_text = (
                                f"{staged.window_start:%d %b %y} – {staged.window_end:%d %b %y}"
                                if staged.window_start and staged.window_end
                                else "—"
                            )
                            ui.label(f"Window: {window_text}").style(f"color: {TEXT_MUTED}; font-size: 11px;")
                            if staged.rationale:
                                ui.label(staged.rationale).style(
                                    f"color: {TEXT_MUTED}; font-size: 11px; font-style: italic;"
                                )
                        ui.button(icon="delete", on_click=lambda i=i: remove_staged(i)).props(
                            "flat dense color=negative"
                        )

        def stage_line():
            aggregate = current_aggregate["value"]
            if aggregate is None or not aggregate.transactions:
                ui.notify("Nothing to stage — adjust the filters above first.", type="negative")
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

            vendor_keys = vendor_select.value or []
            vendor_labels = [v.label for v in vendors if v.key in vendor_keys]
            staged_lines.append(
                StagedLine(
                    description=description,
                    amount=abs(amount_input.value),
                    flow_type=FlowType(flow_select.value),
                    frequency=Frequency(line_frequency_select.value),
                    account_id=target_account_select.value,
                    category_name=category_name,
                    vendor_keys=vendor_keys,
                    vendor_label=" + ".join(vendor_labels),
                    effective_from=dt.date.fromisoformat(from_date_input.value),
                    window_start=aggregate.start_date,
                    window_end=aggregate.end_date,
                )
            )
            render_staged()
            ui.notify(f"Staged “{description}”.", type="positive")

        def remove_staged(i: int):
            if 0 <= i < len(staged_lines):
                del staged_lines[i]
            render_staged()

        def save_all_staged():
            if not staged_lines:
                return
            items = [commit_staged_line(session, staged) for staged in staged_lines]
            session.commit()
            saved_item_ids.update(item.id for item in items)
            count = len(staged_lines)
            staged_lines.clear()
            render_staged()
            refresh_budget_lines()
            ui.notify(f"Saved {count} budget line(s).", type="positive")

        def do_recommend():
            recommendations = recommend_budget(session, account_ids=source_account_select.value or None)
            if not recommendations:
                ui.notify("No recurring bills or material uncaptured spend found.", type="warning")
                return
            staged_lines.clear()
            staged_lines.extend(recommendations)
            render_staged()
            ui.notify(f"Recommended {len(recommendations)} budget line(s) — review before saving.")

        def recommend_clicked():
            if staged_lines:
                with ui.dialog() as confirm_dialog, ui.card():
                    ui.label(
                        f"This will replace the {len(staged_lines)} line(s) already staged with fresh "
                        "recommendations. Continue?"
                    )
                    with ui.row().classes("justify-end w-full gap-2 mt-2"):
                        ui.button("Cancel", on_click=confirm_dialog.close).props("flat")

                        def confirm():
                            confirm_dialog.close()
                            do_recommend()

                        ui.button("Replace", on_click=confirm)
                confirm_dialog.open()
            else:
                do_recommend()

        vendor_select.on_value_change(lambda e: recompute())
        category_select.on_value_change(lambda e: recompute())
        source_account_select.on_value_change(lambda e: recompute())
        interval_select.on_value_change(lambda e: recompute())
        line_frequency_select.on_value_change(lambda e: on_line_frequency_changed())
        from_input.on_value_change(lambda e: recompute())
        to_input.on_value_change(lambda e: recompute())

        render_staged()
        refresh_budget_lines()
