import datetime as dt

from nicegui import ui

from app.forecast import (
    account_run_rate,
    investment_accounts_summary,
    low_balance_warnings,
    monthly_budget_summary,
    monthly_savings_amount,
)
from app.models import Account
from app.web.layout import SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell

WARNING_HORIZON_DAYS = 90


def _money(value: float) -> str:
    return f"£{value:,.2f}"


@ui.page("/")
def dashboard_page():
    session = get_page_session()

    with page_shell("/", "Financial Health Dashboard"):
        accounts = session.query(Account).order_by(Account.name).all()
        account_options = {a.id: a.name for a in accounts}

        with ui.row().classes("items-center gap-2"):
            as_of_input = ui.input("As of", value=dt.date.today().isoformat()).props("type=date")
            exclude_select = (
                ui.select(account_options, multiple=True, label="Exclude accounts", value=[])
                .classes("min-w-[260px]")
                .props("clearable")
            )
            ui.button("Refresh", on_click=lambda: render())

        with ui.row().classes("gap-4 w-full"):
            net_worth_value = _stat_card("Net Worth")
            income_value = _stat_card("Monthly Income")
            expense_value = _stat_card("Monthly Expense")
            savings_value = _stat_card("Savings Rate")

        savings_detail = ui.label().style(f"color: {TEXT_MUTED};")

        ui.label("Investment Returns").classes("text-xl font-bold mt-2")
        investments_table = ui.table(
            columns=[
                {"name": "account", "label": "Account", "field": "account", "align": "left"},
                {"name": "balance", "label": "Balance", "field": "balance", "align": "right"},
                {
                    "name": "annual_rate",
                    "label": "Annualised Return",
                    "field": "annual_rate",
                    "align": "right",
                },
                {
                    "name": "monthly_growth",
                    "label": "Est. Monthly Growth",
                    "field": "monthly_growth",
                    "align": "right",
                },
            ],
            rows=[],
            row_key="account",
        ).classes("w-full")

        ui.label("Accounts At Risk").classes("text-xl font-bold mt-2")
        risk_table = ui.table(
            columns=[
                {"name": "account", "label": "Account", "field": "account", "align": "left"},
                {"name": "net_flow", "label": "Net Monthly Flow", "field": "net_flow", "align": "right"},
                {"name": "status", "label": "Status", "field": "status", "align": "left"},
            ],
            rows=[],
            row_key="account",
        ).classes("w-full")
        risk_empty_label = ui.label("No accounts with a negative run rate or projected overdraft.").style(
            f"color: {SUCCESS};"
        )

        ui.label(f"Low Balance Warnings (next {WARNING_HORIZON_DAYS} days)").classes("text-xl font-bold mt-2")
        warnings_label = ui.label().style("white-space: pre-line;")

        def render():
            as_of = dt.date.fromisoformat(as_of_input.value)
            excluded = list(exclude_select.value or [])
            live_accounts = (
                session.query(Account).filter(Account.id.notin_(excluded)).order_by(Account.name).all()
            )

            net_worth = sum(a.current_balance for a in live_accounts)
            net_worth_value.set_text(_money(net_worth))

            summary = monthly_budget_summary(session, as_of, exclude_account_ids=excluded)
            income = summary.filter(summary["flow_type"] == "Income")["monthly_amount"].sum()
            expense = summary.filter(summary["flow_type"] == "Expense")["monthly_amount"].sum()
            net_remaining = income - expense
            savings_rate = (net_remaining / income) if income else 0.0

            income_value.set_text(_money(income))
            expense_value.set_text(_money(expense))
            savings_value.set_text(f"{savings_rate * 100:.0f}%")
            savings_value.style(
                f"color: {SUCCESS if savings_rate >= 0 else WARNING}; font-size: 22px; font-weight: 700;"
            )

            invested = monthly_savings_amount(session, as_of, exclude_account_ids=excluded)
            savings_detail.set_text(
                f"{_money(net_remaining)}/mo net remaining (income − expense), of which "
                f"{_money(invested)}/mo is being actively invested via recurring transfers."
            )

            investments_table.rows = [
                {
                    "account": row["account"],
                    "balance": _money(row["balance"]),
                    "annual_rate": f"{row['annual_rate'] * 100:.2f}%",
                    "monthly_growth": _money(row["monthly_growth_estimate"]),
                }
                for row in investment_accounts_summary(session, as_of)
                if row["account_id"] not in excluded
            ]
            investments_table.update()

            risk_rows = []
            for account in live_accounts:
                result = account_run_rate(session, account)
                avg = result["avg_monthly_net"]
                overdrawn = None if account.is_credit_card else result["overdrawn_date"]
                if avg >= 0 and overdrawn is None:
                    continue
                status = (
                    f"Projected overdrawn ~{overdrawn.strftime('%d %b %Y')}"
                    if overdrawn
                    else "Declining balance"
                )
                risk_rows.append({"account": account.name, "net_flow": _money(avg), "status": status})
            risk_table.rows = risk_rows
            risk_table.update()
            risk_table.set_visibility(bool(risk_rows))
            risk_empty_label.set_visibility(not risk_rows)

            warnings = low_balance_warnings(
                session, as_of, as_of + dt.timedelta(days=WARNING_HORIZON_DAYS), live_accounts
            )
            if warnings:
                shown = warnings[:8]
                lines = [
                    f"⚠ {w['account']} drops below {_money(w['threshold'])} on "
                    f"{w['date'].strftime('%d %b %Y')} (forecast {_money(w['balance'])})"
                    for w in shown
                ]
                if len(warnings) > len(shown):
                    lines.append(f"…and {len(warnings) - len(shown)} more in this range")
                warnings_label.set_text("\n".join(lines))
                warnings_label.style(f"color: {WARNING}; font-weight: 600; white-space: pre-line;")
            else:
                warnings_label.set_text("No low-balance warnings in this range.")
                warnings_label.style(f"color: {SUCCESS}; white-space: pre-line;")

        as_of_input.on_value_change(lambda e: render())
        exclude_select.on_value_change(lambda e: render())
        render()


def _stat_card(title: str) -> ui.label:
    with ui.column().classes("stat-card"):
        ui.label(title).style(f"color: {TEXT_MUTED}; font-size: 11px;")
        value_label = ui.label("—").style("font-size: 22px; font-weight: 700;")
    return value_label
