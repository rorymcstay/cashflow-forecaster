from nicegui import ui

from app.budget_builder import vendor_options
from app.models import VendorGroup
from app.transactions import query_transactions
from app.vendor_group_recommender import VendorGroupSuggestion, recommend_vendor_groups
from app.vendor_groups import create_vendor_group, list_vendor_groups, update_vendor_group
from app.web.layout import TEXT_MUTED, get_page_session, page_shell


@ui.page("/vendor-groups")
def vendor_groups_page():
    session = get_page_session()

    with page_shell("/vendor-groups", "Vendor Groups"):
        ui.label(
            "Group vendors (merchants) into a named collection — an alternative axis to Category for "
            "the Insights page, e.g. grouping every rideshare app under one line regardless of how "
            "each transaction happened to classify."
        ).style(f"color: {TEXT_MUTED};")

        with ui.row().classes("items-center gap-2"):
            ui.button("+ Add Vendor Group", on_click=lambda: open_group_dialog())
            ui.button("🔮 Suggest Vendor Groups", on_click=lambda: suggest())
            ui.label(
                "Finds recurring vendors that share a category and aren't in any vendor group yet."
            ).style(f"color: {TEXT_MUTED}; font-size: 12px;")

        suggestions_container = ui.column().classes("w-full gap-2")
        suggestions: list[VendorGroupSuggestion] = []

        def render_suggestions():
            suggestions_container.clear()
            with suggestions_container:
                for suggestion in suggestions:
                    with (
                        ui.row()
                        .classes("w-full items-center gap-2")
                        .style(
                            f"background-color: {TEXT_MUTED}1A; border: 1px solid {TEXT_MUTED}; "
                            "border-radius: 6px; padding: 6px 10px;"
                        )
                    ):
                        with ui.column().classes("gap-0").style("flex: 1;"):
                            ui.label(f"🔮 {suggestion.name}").style("font-weight: 600;")
                            ui.label(", ".join(suggestion.vendor_labels)).style(
                                f"color: {TEXT_MUTED}; font-size: 12px;"
                            )
                        ui.button("Accept", on_click=lambda s=suggestion: accept(s)).props("flat dense")
                        ui.button("Dismiss", on_click=lambda s=suggestion: dismiss(s)).props(
                            "flat dense color=negative"
                        )

        def suggest():
            suggestions.clear()
            suggestions.extend(recommend_vendor_groups(session))
            render_suggestions()
            if not suggestions:
                ui.notify("No new vendor group suggestions found.", type="info")

        def accept(suggestion: VendorGroupSuggestion):
            existing = session.query(VendorGroup).filter(VendorGroup.name == suggestion.name).one_or_none()
            if existing is not None:
                merged = sorted(set(existing.vendor_list) | set(suggestion.vendor_keys))
                update_vendor_group(existing, existing.name, merged)
            else:
                create_vendor_group(session, suggestion.name, suggestion.vendor_keys)
            session.commit()
            dismiss(suggestion)
            render_list()

        def dismiss(suggestion: VendorGroupSuggestion):
            if suggestion in suggestions:
                suggestions.remove(suggestion)
            render_suggestions()

        list_container = ui.column().classes("w-full gap-2")

        def render_list():
            list_container.clear()
            groups = list_vendor_groups(session)
            with list_container:
                header = (
                    ui.row()
                    .classes("w-full items-center gap-2 font-bold")
                    .style(f"color: {TEXT_MUTED}; border-bottom: 1px solid #2C313C; padding-bottom: 4px;")
                )
                with header:
                    ui.label("Name").style("width: 200px;")
                    ui.label("Vendors").style("flex: 1;")
                    ui.label("").style("width: 90px;")

                for group in groups:
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(group.name).style("width: 200px;")
                        ui.label(", ".join(group.vendor_list) or "—").style("flex: 1;")
                        with ui.row().classes("gap-1"):
                            ui.button(icon="edit", on_click=lambda g=group: open_group_dialog(g)).props(
                                "flat dense"
                            )
                            ui.button(icon="delete", on_click=lambda g=group: delete_group(g)).props(
                                "flat dense color=negative"
                            )

        def delete_group(group: VendorGroup):
            session.delete(group)
            session.commit()
            render_list()

        def open_group_dialog(group: VendorGroup | None = None):
            transactions = query_transactions(session)
            vendors = vendor_options(transactions)
            vendor_label_by_key = {v.key: f"{v.label} ({v.transaction_count})" for v in vendors}
            # Include any vendor already on this group even if it no longer
            # appears in current transaction history, so editing never
            # silently drops membership the picker can't otherwise show.
            if group is not None:
                for key in group.vendor_list:
                    vendor_label_by_key.setdefault(key, key)

            with ui.dialog() as dialog, ui.card().classes("gap-2 w-full max-w-[480px]"):
                ui.label("Edit Vendor Group" if group else "Add Vendor Group").classes("text-lg font-bold")
                name_input = ui.input("Name", value=group.name if group else "")
                vendor_select = (
                    ui.select(
                        vendor_label_by_key,
                        multiple=True,
                        label="Vendors",
                        value=list(group.vendor_list) if group else [],
                        with_input=True,
                    )
                    .classes("w-full")
                    .props("clearable")
                )

                def save():
                    name = name_input.value.strip()
                    if not name:
                        ui.notify("Please enter a name.", type="negative")
                        return
                    existing = session.query(VendorGroup).filter(VendorGroup.name == name).one_or_none()
                    if existing is not None and existing is not group:
                        ui.notify(f"A vendor group named '{name}' already exists.", type="negative")
                        return

                    vendor_keys = list(vendor_select.value or [])
                    if group is None:
                        create_vendor_group(session, name, vendor_keys)
                    else:
                        update_vendor_group(group, name, vendor_keys)
                    session.commit()
                    dialog.close()
                    render_list()

                with ui.row().classes("justify-end w-full gap-2 mt-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", on_click=save)

            dialog.open()

        render_list()
