import datetime as dt
import tempfile
import uuid
from pathlib import Path

from nicegui import events, ui

from app.models import Account, BudgetSuggestion, Category, Statement, SuggestionStatus, Transaction
from app.seed import get_or_create_category
from app.statement_import import (
    accept_suggestion,
    budget_vs_actual_report,
    import_statement,
    reject_suggestion,
)
from app.statements import extract_csv_transactions
from app.web.layout import SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell


def _money(value: float | None) -> str:
    return "" if value is None else f"£{value:,.2f}"


@ui.page("/statements")
def statements_page():
    session = get_page_session()

    with page_shell("/statements", "Statements"):
        pending_transactions: list[dict] = []
        accounts = session.query(Account).order_by(Account.name).all()
        account_options = {a.id: a.name for a in accounts}

        ui.label(
            "PDF statements aren't parsed here — their layouts vary too much for one parser. "
            "Read them via chat (read_pdf_statement) and import with the import_statement tool instead."
        ).style(f"color: {TEXT_MUTED}; font-size: 12px;")

        with ui.row().classes("items-center gap-4 flex-wrap"):
            import_account_select = ui.select(
                account_options, label="Account", value=next(iter(account_options), None)
            )
            file_label = ui.label("No file chosen").style(f"color: {TEXT_MUTED};")
            start_input = ui.input("Period start", value=dt.date.today().isoformat()).props("type=date")
            end_input = ui.input("Period end", value=dt.date.today().isoformat()).props("type=date")
            import_btn = ui.button("Import", on_click=lambda: do_import())
            import_btn.disable()

        async def handle_upload(e: events.UploadEventArguments):
            tmp_path = Path(tempfile.gettempdir()) / f"upload_{uuid.uuid4().hex}_{e.file.name}"
            await e.file.save(tmp_path)
            try:
                transactions = extract_csv_transactions(str(tmp_path))
            except Exception as exc:
                ui.notify(f"Couldn't read file: {exc}", type="negative")
                return
            finally:
                tmp_path.unlink(missing_ok=True)

            if not transactions:
                ui.notify("That file didn't contain any transactions.", type="negative")
                return

            pending_transactions.clear()
            pending_transactions.extend(transactions)
            file_label.set_text(e.file.name)
            dates = [dt.date.fromisoformat(t["date"]) for t in transactions]
            start_input.value = min(dates).isoformat()
            end_input.value = max(dates).isoformat()
            import_btn.enable()

        ui.upload(label="Choose CSV File…", on_upload=handle_upload, auto_upload=True).props(
            "accept=.csv"
        ).classes("max-w-xs")

        ui.label("Imported Statements").classes("text-xl font-bold mt-2")
        statements_table = ui.table(
            columns=[
                {"name": "account", "label": "Account", "field": "account", "align": "left"},
                {"name": "period", "label": "Period", "field": "period", "align": "left"},
                {"name": "imported", "label": "Imported", "field": "imported", "align": "left"},
                {"name": "count", "label": "Transactions", "field": "count", "align": "right"},
                {"name": "net", "label": "Net", "field": "net", "align": "right"},
            ],
            rows=[],
            row_key="id",
            selection="single",
            pagination=10,
            on_select=lambda e: refresh_detail(selected_statement()),
        ).classes("w-full")

        with ui.row().classes("justify-end w-full"):
            ui.button("Delete Statement", on_click=lambda: delete_statement()).props("flat color=negative")

        ui.label("Budget vs Actual").classes("text-xl font-bold mt-2")
        report_table = ui.table(
            columns=[
                {"name": "category", "label": "Category", "field": "category", "align": "left"},
                {"name": "budgeted", "label": "Budgeted", "field": "budgeted", "align": "right"},
                {"name": "actual", "label": "Actual", "field": "actual", "align": "right"},
                {"name": "variance", "label": "Variance", "field": "variance", "align": "right"},
            ],
            rows=[],
            row_key="category",
        ).classes("w-full")

        ui.label("Transactions (click a category to reclassify)").classes("text-xl font-bold mt-2")
        transactions_container = ui.column().classes("w-full gap-1")

        ui.label("Budget Suggestions").classes("text-xl font-bold mt-2")
        suggestions_container = ui.column().classes("w-full gap-2")

        def do_import():
            if not pending_transactions:
                return
            account_id = import_account_select.value
            if account_id is None:
                ui.notify("Add an account first, on the Accounts page.", type="negative")
                return
            account = session.get(Account, account_id)
            period_start = dt.date.fromisoformat(start_input.value)
            period_end = dt.date.fromisoformat(end_input.value)

            try:
                import_statement(
                    session,
                    account,
                    pending_transactions,
                    period_start,
                    period_end,
                    source_note=file_label.text,
                )
            except ValueError as exc:
                ui.notify(str(exc), type="negative")
                return

            pending_transactions.clear()
            file_label.set_text("No file chosen")
            import_btn.disable()
            refresh_statements()
            refresh_suggestions()

        def refresh_statements():
            statements = session.query(Statement).order_by(Statement.period_start.desc()).all()
            statements_table.rows = [
                {
                    "id": s.id,
                    "account": s.account.name,
                    "period": f"{s.period_start.strftime('%d %b %Y')} – {s.period_end.strftime('%d %b %Y')}",
                    "imported": s.imported_at.strftime("%d %b %Y %H:%M"),
                    "count": len(s.transactions),
                    "net": _money(sum(t.amount for t in s.transactions)),
                }
                for s in statements
            ]
            statements_table.update()
            refresh_detail(None)

        def selected_statement() -> Statement | None:
            if not statements_table.selected:
                return None
            return session.get(Statement, statements_table.selected[0]["id"])

        def delete_statement():
            statement = selected_statement()
            if statement is None:
                ui.notify("Select a statement to delete first.", type="warning")
                return
            account = statement.account
            account.current_balance -= sum(t.amount for t in statement.transactions)
            session.delete(statement)
            session.commit()
            statements_table.selected = []
            refresh_statements()

        def reclassify(transaction: Transaction):
            categories = sorted({c.name for c in session.query(Category).all()})
            with ui.dialog() as dialog, ui.card().classes("gap-2 min-w-[360px]"):
                ui.label("Reclassify Transaction").classes("text-lg font-bold")
                ui.label(transaction.description)
                ui.label(_money(transaction.amount))
                category_input = ui.select(
                    categories,
                    label="Category",
                    value=(transaction.category.name if transaction.category else None),
                    with_input=True,
                    new_value_mode="add-unique",
                )

                def save():
                    category_name = (category_input.value or "").strip()
                    if not category_name:
                        ui.notify("Please enter or choose a category.", type="negative")
                        return
                    transaction.category = get_or_create_category(session, category_name)
                    session.commit()
                    dialog.close()
                    refresh_detail(transaction.statement)

                with ui.row().classes("justify-end w-full gap-2 mt-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", on_click=save)
            dialog.open()

        def refresh_detail(statement: Statement | None):
            report_table.rows = []
            transactions_container.clear()
            if statement is None:
                report_table.update()
                return

            report = budget_vs_actual_report(session, statement)
            rows = [
                {
                    "category": row["category"],
                    "budgeted": _money(row["budgeted"]),
                    "actual": _money(row["actual"]),
                    "variance": _money(row["variance"]),
                }
                for row in report["by_category"]
            ]
            rows.append(
                {
                    "category": "Total",
                    "budgeted": _money(report["total_budgeted"]),
                    "actual": _money(report["total_actual"]),
                    "variance": _money(report["total_variance"]),
                }
            )
            report_table.rows = rows
            report_table.update()

            with transactions_container:
                header = (
                    ui.row().classes("w-full items-center gap-2 font-bold").style(f"color: {TEXT_MUTED};")
                )
                with header:
                    ui.label("Date").style("width: 100px;")
                    ui.label("Description").style("width: 220px;")
                    ui.label("Amount").style("width: 100px;")
                    ui.label("Category").style("width: 130px;")
                    ui.label("Matched Budget Item").style("flex: 1;")
                for t in sorted(statement.transactions, key=lambda t: t.date):
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(t.date.strftime("%d %b %Y")).style("width: 100px;")
                        ui.label(t.description).style("width: 220px;")
                        ui.label(_money(t.amount)).style("width: 100px;")
                        ui.button(
                            t.category.name if t.category else "—", on_click=lambda t=t: reclassify(t)
                        ).props("flat dense no-caps").style("width: 130px; justify-content: flex-start;")
                        ui.label(t.matched_budget_item.description if t.matched_budget_item else "—").style(
                            "flex: 1;"
                        )

        def refresh_suggestions():
            suggestions_container.clear()
            suggestions = (
                session.query(BudgetSuggestion)
                .filter_by(status=SuggestionStatus.PENDING)
                .order_by(BudgetSuggestion.created_at.desc())
                .all()
            )
            with suggestions_container:
                if not suggestions:
                    ui.label("No pending suggestions.").style(f"color: {TEXT_MUTED};")
                for sg in suggestions:
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(sg.suggestion_type.value).style("width: 110px;")
                        ui.label(sg.description).style("width: 160px;")
                        ui.label(sg.category.name).style("width: 100px;")
                        ui.label(sg.account.name).style("width: 130px;")
                        ui.label(_money(sg.current_amount) or "—").style("width: 90px;")
                        ui.label(_money(sg.proposed_amount)).style("width: 90px;")
                        ui.label(sg.rationale or "").style(f"flex: 1; color: {TEXT_MUTED};")
                        ui.button("Accept", on_click=lambda sg=sg: do_accept(sg)).props(
                            "flat dense color=positive"
                        )
                        ui.button("Reject", on_click=lambda sg=sg: do_reject(sg)).props(
                            "flat dense color=negative"
                        )

        def do_accept(sg: BudgetSuggestion):
            accept_suggestion(session, sg)
            refresh_suggestions()

        def do_reject(sg: BudgetSuggestion):
            reject_suggestion(session, sg)
            refresh_suggestions()

        refresh_statements()
        refresh_suggestions()
