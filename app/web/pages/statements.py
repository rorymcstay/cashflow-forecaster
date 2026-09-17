import datetime as dt
import tempfile
import uuid
from pathlib import Path

from nicegui import events, run, ui

from app.models import Account, BudgetSuggestion, Category, Statement, SuggestionStatus, Transaction
from app.pdf_statement_parsers import parse_pdf_statement
from app.seed import get_or_create_category
from app.statement_import import (
    accept_suggestion,
    budget_vs_actual_report,
    detect_account_for_hint,
    find_statement_gaps,
    import_statement,
    reject_suggestion,
)
from app.statements import extract_csv_transactions
from app.web.layout import BORDER, SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell

_KIND_LABELS = {"hsbc_premier": "HSBC Premier PDF", "amex": "Amex PDF", "csv": "CSV"}


def _money(value: float | None) -> str:
    return "" if value is None else f"£{value:,.2f}"


@ui.page("/statements")
def statements_page():
    session = get_page_session()

    with page_shell("/statements", "Statements"):
        ui.label(
            "CSV and PDF statements are both supported here. PDFs are only structurally parsed for "
            "HSBC Premier and Amex layouts — anything else falls back to reading it via chat "
            "(read_pdf_statement) and importing with the import_statement tool instead."
        ).style(f"color: {TEXT_MUTED}; font-size: 12px;")

        with (
            ui.column()
            .classes("w-full items-center gap-2")
            .style(f"border: 2px dashed {BORDER}; border-radius: 8px; padding: 20px;")
        ):
            ui.icon("cloud_upload").classes("text-3xl").style(f"color: {TEXT_MUTED};")
            ui.label("Drag & drop CSV or PDF statements here, or click to browse").style(
                f"color: {TEXT_MUTED};"
            )
            upload_widget = (
                ui.upload(auto_upload=True, multiple=True).props("accept=.csv,.pdf").classes("max-w-md")
            )

        pending_entries: list[dict] = []

        with ui.row().classes("items-center justify-between w-full mt-2"):
            ui.label("Pending Imports").classes("text-xl font-bold")
            import_all_btn = ui.button("Import All", on_click=lambda: import_all()).props("color=primary")
            import_all_btn.set_visibility(False)
        pending_container = ui.column().classes("w-full gap-2")
        pending_placeholder = ui.label("Drop a file above to see it here before importing.").style(
            f"color: {TEXT_MUTED};"
        )

        def refresh_pending_controls():
            count = len(pending_entries)
            import_all_btn.set_visibility(count > 0)
            import_all_btn.set_text(f"Import All ({count})" if count else "Import All")

        def run_import(entry: dict) -> tuple[bool, str]:
            """Import one pending entry. Returns (ok, message) — used by both
            the per-card Import button and Import All, so both report the
            exact same success/historical/failure wording."""
            account_id = entry["account_select"].value
            if account_id is None:
                return False, "Choose an account first."
            account = session.get(Account, account_id)
            if account is None:
                return False, "Choose an account first."
            period_start = dt.date.fromisoformat(entry["start_input"].value)
            period_end = dt.date.fromisoformat(entry["end_input"].value)
            is_reimport = (
                session.query(Statement)
                .filter_by(account_id=account.id, period_start=period_start, period_end=period_end)
                .first()
                is not None
            )
            balance_before = account.current_balance
            try:
                import_statement(
                    session,
                    account,
                    entry["parsed"]["transactions"],
                    period_start,
                    period_end,
                    source_note=entry["filename"],
                    closing_balance=entry["parsed"].get("closing_balance"),
                )
            except ValueError as exc:
                return False, str(exc)
            verb = "Updated" if is_reimport else "Imported"
            if account.current_balance == balance_before:
                return True, (
                    f"{verb} {entry['filename']} as historical — balance already known as of "
                    f"{account.balance_as_of.strftime('%d %b %Y')}, so it wasn't changed."
                )
            return True, f"{verb} {entry['filename']} — balance now {_money(account.current_balance)}."

        def import_all():
            # Chronological order matters: the same-account balance math
            # (front/historical/forward-extends) depends on each import
            # seeing the account's balance_as_of as it would after every
            # earlier-dated statement has already landed.
            entries = sorted(pending_entries, key=lambda e: dt.date.fromisoformat(e["start_input"].value))
            if not entries:
                return
            results = [(entry["filename"], *run_import(entry)) for entry in entries]
            for entry, (_filename, ok, _msg) in zip(entries, results, strict=True):
                if ok:
                    entry["discard"]()
            refresh_statements()
            refresh_suggestions()
            refresh_gaps()
            successes = sum(1 for _, ok, _ in results if ok)
            failures = [(fn, msg) for fn, ok, msg in results if not ok]
            summary = f"Imported {successes} of {len(results)} statement(s)."
            if failures:
                summary += " Not imported: " + "; ".join(f"{fn} ({msg})" for fn, msg in failures)
            ui.notify(summary, type="warning" if failures else "positive", multi_line=True)

        async def handle_upload(e: events.UploadEventArguments):
            tmp_path = Path(tempfile.gettempdir()) / f"upload_{uuid.uuid4().hex}_{e.file.name}"
            await e.file.save(tmp_path)
            suffix = Path(e.file.name).suffix.lower()
            try:
                if suffix == ".csv":
                    transactions = await run.io_bound(extract_csv_transactions, str(tmp_path))
                    parsed = {
                        "kind": "csv",
                        "transactions": transactions or [],
                        "period_start": None,
                        "period_end": None,
                        "account_hint": None,
                        "reconciliation": None,
                    }
                elif suffix == ".pdf":
                    parsed = await run.io_bound(parse_pdf_statement, str(tmp_path)) or {
                        "kind": None,
                        "transactions": [],
                    }
                else:
                    ui.notify(f"Unsupported file type: {e.file.name}", type="negative")
                    return
            except Exception as exc:
                ui.notify(f"Couldn't read {e.file.name}: {exc}", type="negative")
                return
            finally:
                tmp_path.unlink(missing_ok=True)

            if not parsed["transactions"]:
                if suffix == ".pdf" and parsed.get("kind") is None:
                    ui.notify(
                        f"Couldn't recognise the PDF format of {e.file.name} — read it via chat "
                        "(read_pdf_statement) and import with the import_statement tool instead.",
                        type="warning",
                    )
                else:
                    ui.notify(f"{e.file.name} didn't contain any transactions.", type="negative")
                return

            add_pending_card(e.file.name, parsed)

        upload_widget.on_upload(handle_upload)

        def add_pending_card(filename: str, parsed: dict):
            pending_placeholder.set_visibility(False)
            accounts = session.query(Account).order_by(Account.name).all()
            account_options = {a.id: a.name for a in accounts}
            hint = parsed.get("account_hint")
            default_account = detect_account_for_hint(accounts, hint)

            dates = [dt.date.fromisoformat(t["date"]) for t in parsed["transactions"]]
            period_start = (
                dt.date.fromisoformat(parsed["period_start"]) if parsed.get("period_start") else min(dates)
            )
            period_end = (
                dt.date.fromisoformat(parsed["period_end"]) if parsed.get("period_end") else max(dates)
            )

            with pending_container:
                card = ui.card().classes("w-full")
            with card:
                with ui.row().classes("items-center gap-3 flex-wrap w-full"):
                    ui.icon("description")
                    ui.label(filename).classes("font-bold")
                    ui.badge(_KIND_LABELS.get(parsed.get("kind"), "PDF")).props(
                        "color=primary" if parsed.get("kind") else "color=grey"
                    )
                    if hint:
                        if default_account is not None:
                            ui.label(f"Detected account: {account_options[default_account]}").style(
                                f"color: {TEXT_MUTED};"
                            )
                        else:
                            ui.label(f"Detected bank: {hint} — choose the account below").style(
                                f"color: {WARNING};"
                            )

                with ui.row().classes("items-center gap-3 flex-wrap w-full mt-1"):
                    account_select = ui.select(
                        account_options, label="Account", value=default_account
                    ).classes("min-w-[180px]")
                    start_input = ui.input("Period start", value=period_start.isoformat()).props("type=date")
                    end_input = ui.input("Period end", value=period_end.isoformat()).props("type=date")
                    ui.label(f"{len(parsed['transactions'])} transactions").style(f"color: {TEXT_MUTED};")

                recon = parsed.get("reconciliation")
                if recon is not None:
                    if recon["ok"]:
                        ui.label(
                            f"✓ Reconciles with the statement's own summary — "
                            f"in {_money(recon['actual_in'])}, out {_money(recon['actual_out'])}"
                        ).style(f"color: {SUCCESS}; font-size: 12px;")
                    else:
                        ui.label(
                            "⚠ Doesn't reconcile with the statement's own summary — expected in "
                            f"{_money(recon['expected_in'])} / out {_money(recon['expected_out'])}, parsed in "
                            f"{_money(recon['actual_in'])} / out {_money(recon['actual_out'])}. Review before "
                            "importing."
                        ).style(f"color: {WARNING}; font-size: 12px;")

                with ui.row().classes("justify-end w-full gap-2 mt-1"):
                    ui.button("Discard", on_click=lambda: discard()).props("flat")
                    ui.button("Import", on_click=lambda: do_import()).props("color=primary")

            entry = {
                "filename": filename,
                "parsed": parsed,
                "account_select": account_select,
                "start_input": start_input,
                "end_input": end_input,
                "card": card,
            }

            def discard():
                pending_container.remove(card)
                if entry in pending_entries:
                    pending_entries.remove(entry)
                refresh_pending_controls()

            entry["discard"] = discard
            pending_entries.append(entry)
            refresh_pending_controls()

            def do_import():
                ok, msg = run_import(entry)
                if not ok:
                    ui.notify(msg, type="negative")
                    return
                discard()
                refresh_statements()
                refresh_suggestions()
                refresh_gaps()
                ui.notify(msg, type="positive")

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

        ui.label("Missing Statements").classes("text-xl font-bold mt-2")
        all_accounts = session.query(Account).order_by(Account.name).all()
        accounts_with_history = {row[0] for row in session.query(Statement.account_id).distinct().all()}
        with ui.row().classes("items-center gap-2 flex-wrap"):
            gap_account_select = (
                ui.select(
                    {a.id: a.name for a in all_accounts},
                    label="Accounts",
                    multiple=True,
                    value=[a.id for a in all_accounts if a.id in accounts_with_history],
                )
                .classes("min-w-[190px]")
                .props("use-chips")
            )
            gap_start_input = ui.input(
                "From", value=(dt.date.today() - dt.timedelta(days=365)).isoformat()
            ).props("type=date")
            gap_end_input = ui.input("To", value=dt.date.today().isoformat()).props("type=date")
            ui.button("Check", on_click=lambda: refresh_gaps())
        gaps_container = ui.column().classes("w-full gap-1")

        def refresh_gaps():
            gaps_container.clear()
            try:
                start = dt.date.fromisoformat(gap_start_input.value)
                end = dt.date.fromisoformat(gap_end_input.value)
            except (ValueError, TypeError):
                return
            account_ids = gap_account_select.value or []
            with gaps_container:
                found_any = False
                for account_id in account_ids:
                    account = session.get(Account, account_id)
                    if account is None:
                        continue
                    gaps = find_statement_gaps(session, account_id, start, end)
                    if not gaps:
                        continue
                    found_any = True
                    with ui.row().classes("w-full items-start gap-2"):
                        ui.label(account.name).classes("font-bold").style("width: 160px;")
                        ui.label(", ".join(g["label"] for g in gaps)).style(f"color: {WARNING};")
                if not found_any:
                    ui.label(
                        "No gaps — every selected account has statement coverage across this range."
                    ).style(f"color: {SUCCESS};")

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
            refresh_gaps()

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
        refresh_gaps()
