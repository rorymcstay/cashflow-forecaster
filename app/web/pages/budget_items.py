import datetime as dt

from nicegui import ui

from app.models import Account, BudgetItem, Category, FlowType, Frequency
from app.seed import get_or_create_category
from app.web.layout import TEXT_MUTED, get_page_session, page_shell


def _money(value: float) -> str:
    return f"£{value:,.2f}"


@ui.page("/budget-items")
def budget_items_page():
    session = get_page_session()

    with page_shell("/budget-items", "Budget Items"):
        ui.button("+ Add Budget Item", on_click=lambda: open_item_dialog())
        list_container = ui.column().classes("w-full gap-2")

        def render_list():
            list_container.clear()
            items = session.query(BudgetItem).order_by(BudgetItem.description).all()
            with list_container:
                header = (
                    ui.row()
                    .classes("w-full items-center gap-2 font-bold")
                    .style(f"color: {TEXT_MUTED}; border-bottom: 1px solid #2C313C; padding-bottom: 4px;")
                )
                with header:
                    ui.label("Description").style("width: 200px;")
                    ui.label("Amount").style("width: 100px;")
                    ui.label("Type").style("width: 90px;")
                    ui.label("Frequency").style("width: 100px;")
                    ui.label("Category").style("width: 120px;")
                    ui.label("Account").style("flex: 1;")
                    ui.label("").style("width: 90px;")

                for item in items:
                    account_label = item.account.name
                    if item.flow_type == FlowType.TRANSFER and item.target_account:
                        account_label = f"{item.account.name} → {item.target_account.name}"
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(item.description).style("width: 200px;")
                        ui.label(_money(item.amount)).style("width: 100px;")
                        ui.label(item.flow_type.value).style("width: 90px;")
                        ui.label(item.frequency.value).style("width: 100px;")
                        ui.label(item.category.name).style("width: 120px;")
                        ui.label(account_label).style("flex: 1;")
                        with ui.row().classes("gap-1"):
                            ui.button(icon="edit", on_click=lambda i=item: open_item_dialog(i)).props(
                                "flat dense"
                            )
                            ui.button(icon="delete", on_click=lambda i=item: delete_item(i)).props(
                                "flat dense color=negative"
                            )

        def delete_item(item: BudgetItem):
            session.delete(item)
            session.commit()
            render_list()

        def open_item_dialog(item: BudgetItem | None = None):
            accounts = session.query(Account).order_by(Account.name).all()
            account_options = {a.id: a.name for a in accounts}
            categories = sorted({c.name for c in session.query(Category).all()})

            with ui.dialog() as dialog, ui.card().classes("gap-2 w-full max-w-[420px]"):
                ui.label("Edit Budget Item" if item else "Add Budget Item").classes("text-lg font-bold")
                desc_input = ui.input("Description", value=item.description if item else "")
                amount_input = ui.number("Amount", value=item.amount if item else 0.0, format="%.2f")
                flow_select = ui.select(
                    {ft.value: ft.value for ft in FlowType},
                    label="Type",
                    value=(item.flow_type.value if item else FlowType.EXPENSE.value),
                )
                freq_select = ui.select(
                    {f.value: f.value for f in Frequency},
                    label="Frequency",
                    value=(item.frequency.value if item else Frequency.MONTHLY.value),
                )
                from_input = ui.input(
                    "Effective From", value=(item.effective_from if item else dt.date.today()).isoformat()
                ).props("type=date")
                has_until_check = ui.checkbox("Has an end date", value=bool(item and item.effective_until))
                until_input = (
                    ui.input(
                        "Effective Until",
                        value=(
                            item.effective_until if item and item.effective_until else dt.date.today()
                        ).isoformat(),
                    )
                    .props("type=date")
                    .bind_visibility_from(has_until_check, "value")
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
                notes_input = ui.input("Notes", value=item.notes or "" if item else "")

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

                    effective_from = dt.date.fromisoformat(from_input.value)
                    effective_until = (
                        dt.date.fromisoformat(until_input.value) if has_until_check.value else None
                    )
                    if effective_until is not None and effective_until < effective_from:
                        ui.notify("Effective Until must be on or after Effective From.", type="negative")
                        return

                    flow_type = FlowType(flow_select.value)
                    target_account_id = target_select.value if flow_type == FlowType.TRANSFER else None
                    if flow_type == FlowType.TRANSFER and target_account_id is None:
                        ui.notify("Choose an account to transfer to.", type="negative")
                        return

                    category = get_or_create_category(session, category_name)

                    obj = item
                    if obj is None:
                        obj = BudgetItem(
                            description=description, category=category, account_id=account_select.value
                        )
                        session.add(obj)

                    obj.description = description
                    obj.amount = amount_input.value
                    obj.flow_type = flow_type
                    obj.frequency = Frequency(freq_select.value)
                    obj.effective_from = effective_from
                    obj.effective_until = effective_until
                    obj.notes = notes_input.value.strip() or None
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
