import datetime as dt

from nicegui import ui

from app.models import Account, Category, FlowType, UpcomingExpense, UpcomingExpenseStatus
from app.seed import get_or_create_category
from app.web.layout import TEXT_MUTED, WARNING, get_page_session, page_shell


def _money(value: float) -> str:
    return f"£{value:,.2f}"


@ui.page("/upcoming-expenses")
def upcoming_expenses_page():
    session = get_page_session()

    with page_shell("/upcoming-expenses", "Upcoming Expenses"):
        selected_ids: set[int] = set()

        with ui.row().classes("items-center gap-2"):
            ui.button("+ Add Upcoming Expense", on_click=lambda: open_item_dialog())
            ui.button("Archive Selected", on_click=lambda: archive_selected()).props("flat")
        ui.label("Check the box on items to archive, or click the pencil to edit/reschedule.").style(
            f"color: {TEXT_MUTED};"
        )

        list_container = ui.column().classes("w-full gap-2")

        def render_list():
            list_container.clear()
            items = session.query(UpcomingExpense).order_by(UpcomingExpense.date).all()
            with list_container:
                header = (
                    ui.row()
                    .classes("w-full items-center gap-2 font-bold")
                    .style(f"color: {TEXT_MUTED}; border-bottom: 1px solid #2C313C; padding-bottom: 4px;")
                )
                with header:
                    ui.label("").style("width: 30px;")
                    ui.label("Date").style("width: 100px;")
                    ui.label("Description").style("width: 180px;")
                    ui.label("Amount").style("width: 100px;")
                    ui.label("Type").style("width: 80px;")
                    ui.label("Status").style("width: 110px;")
                    ui.label("Category").style("width: 110px;")
                    ui.label("Account").style("flex: 1;")
                    ui.label("").style("width: 90px;")

                for item in items:
                    account_label = item.account.name
                    if item.flow_type == FlowType.TRANSFER and item.target_account:
                        account_label = f"{item.account.name} → {item.target_account.name}"
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.checkbox(value=item.id in selected_ids).on_value_change(
                            lambda e, i=item.id: selected_ids.add(i) if e.value else selected_ids.discard(i)
                        ).style("width: 30px;")
                        ui.label(item.date.strftime("%d %b %Y")).style("width: 100px;")
                        ui.label(item.description).style("width: 180px;")
                        ui.label(_money(item.amount)).style("width: 100px;")
                        ui.label(item.flow_type.value).style("width: 80px;")
                        status_color = (
                            WARNING if item.status == UpcomingExpenseStatus.NEEDS_REVIEW else TEXT_MUTED
                        )
                        ui.label(item.status.value).style(f"width: 110px; color: {status_color};")
                        ui.label(item.category.name).style("width: 110px;")
                        ui.label(account_label).style("flex: 1;")
                        with ui.row().classes("gap-1"):
                            ui.button(icon="edit", on_click=lambda i=item: open_item_dialog(i)).props(
                                "flat dense"
                            )
                            ui.button(icon="delete", on_click=lambda i=item: delete_item(i)).props(
                                "flat dense color=negative"
                            )

        def delete_item(item: UpcomingExpense):
            session.delete(item)
            session.commit()
            render_list()

        def archive_selected():
            if not selected_ids:
                ui.notify("Check one or more items to archive first.", type="warning")
                return
            for item_id in list(selected_ids):
                item = session.get(UpcomingExpense, item_id)
                if item is not None:
                    item.status = UpcomingExpenseStatus.ARCHIVED
            session.commit()
            selected_ids.clear()
            render_list()

        def open_item_dialog(item: UpcomingExpense | None = None):
            accounts = session.query(Account).order_by(Account.name).all()
            account_options = {a.id: a.name for a in accounts}
            categories = sorted({c.name for c in session.query(Category).all()})

            with ui.dialog() as dialog, ui.card().classes("gap-2 min-w-[420px]"):
                ui.label("Edit Upcoming Expense" if item else "Add Upcoming Expense").classes(
                    "text-lg font-bold"
                )
                date_input = ui.input(
                    "Date", value=(item.date if item else dt.date.today()).isoformat()
                ).props("type=date")
                desc_input = ui.input("Description", value=item.description if item else "")
                amount_input = ui.number("Amount", value=item.amount if item else 0.0, format="%.2f")
                flow_select = ui.select(
                    {ft.value: ft.value for ft in FlowType},
                    label="Type",
                    value=(item.flow_type.value if item else FlowType.EXPENSE.value),
                )
                category_input = ui.select(
                    categories,
                    label="Category",
                    value=(item.category.name if item else None),
                    with_input=True,
                    new_value_mode="add-unique",
                )
                account_select = ui.select(
                    account_options, label="Account", value=(item.account_id if item else None)
                )
                with (
                    ui.column()
                    .bind_visibility_from(
                        flow_select, "value", backward=lambda v: v == FlowType.TRANSFER.value
                    )
                    .classes("w-full")
                ):
                    target_select = ui.select(
                        account_options,
                        label="Transfer To",
                        value=(item.target_account_id if item and item.target_account_id else None),
                    )
                if item and item.status == UpcomingExpenseStatus.NEEDS_REVIEW:
                    ui.label("Needs Review — changing the date will reschedule it to Pending").style(
                        f"color: {WARNING};"
                    )

                def save():
                    description = desc_input.value.strip()
                    if not description:
                        ui.notify("Please enter a description.", type="negative")
                        return
                    if not account_options:
                        ui.notify("Add an account first, on the Accounts page.", type="negative")
                        return
                    category_name = (category_input.value or "").strip()
                    if not category_name:
                        ui.notify("Please enter or choose a category.", type="negative")
                        return

                    flow_type = FlowType(flow_select.value)
                    target_account_id = target_select.value if flow_type == FlowType.TRANSFER else None
                    if flow_type == FlowType.TRANSFER and target_account_id is None:
                        ui.notify("Choose an account to transfer to.", type="negative")
                        return

                    category = get_or_create_category(session, category_name)
                    new_date = dt.date.fromisoformat(date_input.value)

                    obj = item
                    if obj is None:
                        obj = UpcomingExpense(
                            date=new_date,
                            description=description,
                            category=category,
                            account_id=account_select.value,
                        )
                        session.add(obj)
                    else:
                        if new_date != obj.date and obj.status == UpcomingExpenseStatus.NEEDS_REVIEW:
                            obj.status = UpcomingExpenseStatus.PENDING
                            obj.matched_transaction = None
                            obj.last_seen_statement = None

                    obj.date = new_date
                    obj.description = description
                    obj.amount = amount_input.value
                    obj.flow_type = flow_type
                    obj.category = category
                    obj.account_id = account_select.value
                    obj.target_account_id = target_account_id

                    session.commit()
                    dialog.close()
                    render_list()

                with ui.row().classes("justify-end w-full gap-2 mt-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", on_click=save)

            dialog.open()

        render_list()
