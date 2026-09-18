from nicegui import ui

from app.budgets import (
    archive_budget,
    create_budget,
    delete_budget,
    get_active_budget,
    list_budgets,
    rename_budget,
    restore_budget,
    set_active_budget,
)
from app.models import Budget, BudgetStatus
from app.web.layout import TEXT_MUTED, get_page_session, page_shell


@ui.page("/budgets")
def budgets_page():
    session = get_page_session()

    with page_shell("/budgets", "Budgets"):
        ui.label(
            "Exactly one budget is active at a time — every screen and forecast reads only its "
            "budget items. “Save Active As” branches the active budget under a new name; "
            "archiving hides a budget from active use without deleting its history."
        ).style(f"color: {TEXT_MUTED};")

        with ui.row().classes("gap-2"):
            ui.button("+ New Budget", on_click=lambda: open_name_dialog("New Budget", on_create))
            ui.button(
                "Save Active As…",
                on_click=lambda: open_name_dialog(
                    "Save Active Budget As",
                    on_save_as,
                    default=f"{get_active_budget(session).name} copy",
                ),
            )

        list_container = ui.column().classes("w-full gap-2")

        def name_in_use(name: str, ignore: Budget | None = None) -> bool:
            existing = session.query(Budget).filter(Budget.name == name).one_or_none()
            return existing is not None and existing is not ignore

        def open_name_dialog(title: str, on_confirm, default: str = ""):
            with ui.dialog() as dialog, ui.card().classes("gap-2 w-full max-w-[360px]"):
                ui.label(title).classes("text-lg font-bold")
                name_input = ui.input("Name", value=default).classes("w-full")

                def confirm():
                    name = (name_input.value or "").strip()
                    if not name:
                        ui.notify("Please enter a name.", type="negative")
                        return
                    on_confirm(name, dialog)

                with ui.row().classes("justify-end w-full gap-2 mt-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", on_click=confirm)
            dialog.open()

        def on_create(name: str, dialog) -> None:
            if name_in_use(name):
                ui.notify(f"A budget named '{name}' already exists.", type="negative")
                return
            create_budget(session, name)
            session.commit()
            dialog.close()
            render_list()

        def on_save_as(name: str, dialog) -> None:
            if name_in_use(name):
                ui.notify(f"A budget named '{name}' already exists.", type="negative")
                return
            create_budget(session, name, clone_from=get_active_budget(session))
            session.commit()
            dialog.close()
            render_list()

        def activate(budget: Budget) -> None:
            set_active_budget(session, budget.id)
            session.commit()
            ui.navigate.reload()

        def rename(budget: Budget) -> None:
            def on_rename(name: str, dialog) -> None:
                if name_in_use(name, ignore=budget):
                    ui.notify(f"A budget named '{name}' already exists.", type="negative")
                    return
                rename_budget(budget, name)
                session.commit()
                dialog.close()
                render_list()

            open_name_dialog("Rename Budget", on_rename, default=budget.name)

        def archive(budget: Budget) -> None:
            try:
                archive_budget(session, budget)
                session.commit()
            except ValueError as e:
                session.rollback()
                ui.notify(str(e), type="negative")
                return
            render_list()

        def restore(budget: Budget) -> None:
            restore_budget(budget)
            session.commit()
            render_list()

        def delete(budget: Budget) -> None:
            try:
                delete_budget(session, budget)
                session.commit()
            except ValueError as e:
                session.rollback()
                ui.notify(str(e), type="negative")
                return
            render_list()

        def render_list():
            list_container.clear()
            active = get_active_budget(session)
            budgets = list_budgets(session)
            with list_container:
                header = (
                    ui.row()
                    .classes("w-full items-center gap-2 font-bold")
                    .style(f"color: {TEXT_MUTED}; border-bottom: 1px solid #2C313C; padding-bottom: 4px;")
                )
                with header:
                    ui.label("Name").style("width: 220px;")
                    ui.label("Status").style("width: 100px;")
                    ui.label("Budget Items").style("width: 110px;")
                    ui.label("Created").style("width: 110px;")
                    ui.label("").style("flex: 1;")

                for budget in budgets:
                    is_active = budget.id == active.id
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(("★ " if is_active else "") + budget.name).style("width: 220px;")
                        ui.label(budget.status.value).style("width: 100px;")
                        ui.label(str(len(budget.budget_items))).style("width: 110px;")
                        ui.label(budget.created_at.strftime("%d %b %Y")).style("width: 110px;")
                        with ui.row().classes("gap-1").style("flex: 1;"):
                            if not is_active:
                                ui.button("Activate", on_click=lambda b=budget: activate(b)).props(
                                    "flat dense"
                                )
                            ui.button("Rename", on_click=lambda b=budget: rename(b)).props("flat dense")
                            if budget.status == BudgetStatus.ACTIVE:
                                ui.button("Archive", on_click=lambda b=budget: archive(b)).props("flat dense")
                            else:
                                ui.button("Restore", on_click=lambda b=budget: restore(b)).props("flat dense")
                            if not is_active:
                                ui.button(icon="delete", on_click=lambda b=budget: delete(b)).props(
                                    "flat dense color=negative"
                                )

        render_list()
