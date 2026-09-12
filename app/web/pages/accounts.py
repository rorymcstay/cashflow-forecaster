import datetime as dt

from nicegui import ui

from app.forecast import account_daily_forecast, account_run_rate
from app.models import Account, BudgetItem, Holding, UpcomingExpense
from app.web.layout import SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell

FORECAST_DAYS = 30


def _money(value: float) -> str:
    return f"£{value:,.2f}"


def _forecast_balance(session, account: Account, days: int = FORECAST_DAYS) -> float:
    today = dt.date.today()
    df = account_daily_forecast(session, account, today, today + dt.timedelta(days=days))
    if df.height == 0:
        return account.current_balance
    return float(df["balance"][-1])


def _cc_autopay_label(account: Account) -> str:
    if not account.is_credit_card:
        return "—"
    if account.cc_payee_account is None or account.cc_payment_day is None:
        return "Credit card (autopay not set up)"
    payment = "in full" if account.cc_pay_in_full else _money(account.cc_fixed_payment_amount or 0)
    return f"{payment} from {account.cc_payee_account.name} on day {account.cc_payment_day}"


def _parse_holdings(text: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for part in text.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        ticker, _, weight = part.partition(":")
        ticker = ticker.strip().upper()
        try:
            w = float(weight.strip())
        except ValueError:
            continue
        if ticker and w > 0:
            weights[ticker] = weights.get(ticker, 0.0) + w
    return weights


def _format_holdings(account: Account) -> str:
    return ", ".join(f"{h.ticker}:{h.weight:g}" for h in account.holdings)


@ui.page("/accounts")
def accounts_page():
    session = get_page_session()

    with page_shell("/accounts", "Accounts"):
        ui.button("+ Add Account", on_click=lambda: open_account_dialog())
        list_container = ui.column().classes("w-full gap-2")

        def render_list():
            list_container.clear()
            accounts = session.query(Account).order_by(Account.name).all()
            with list_container:
                header = (
                    ui.row()
                    .classes("w-full items-center gap-2 font-bold")
                    .style(f"color: {TEXT_MUTED}; border-bottom: 1px solid #2C313C; padding-bottom: 4px;")
                )
                with header:
                    ui.label("Name").style("width: 160px;")
                    ui.label("Balance").style("width: 110px;")
                    ui.label("As Of").style("width: 100px;")
                    ui.label("Net Monthly Flow").style("width: 130px;")
                    ui.label(f"{FORECAST_DAYS}-Day Forecast").style("width: 130px;")
                    ui.label("Growth / Autopay").style("flex: 1;")
                    ui.label("Holdings").style("width: 140px;")
                    ui.label("").style("width: 110px;")

                for account in accounts:
                    net = account_run_rate(session, account)["avg_monthly_net"]
                    forecast_balance = _forecast_balance(session, account)
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(account.name).style("width: 160px;")
                        ui.label(_money(account.current_balance)).style("width: 110px;")
                        ui.label(account.balance_as_of.strftime("%d %b %Y")).style("width: 100px;")
                        flow_color = SUCCESS if net >= 0 else WARNING
                        ui.label(f"{_money(net)}/mo").style(f"width: 130px; color: {flow_color};")
                        forecast_color = SUCCESS if forecast_balance >= 0 else WARNING
                        ui.label(_money(forecast_balance)).style(f"width: 130px; color: {forecast_color};")
                        detail = (
                            _cc_autopay_label(account)
                            if account.is_credit_card
                            else (f"{account.growth_rate:.2f}% APY" if account.growth_rate else "—")
                        )
                        ui.label(detail).style(f"flex: 1; color: {TEXT_MUTED};")
                        ui.label(_format_holdings(account) or "—").style("width: 140px;")
                        with ui.row().classes("gap-1"):
                            ui.button(icon="edit", on_click=lambda a=account: open_account_dialog(a)).props(
                                "flat dense"
                            )
                            ui.button(icon="delete", on_click=lambda a=account: delete_account(a)).props(
                                "flat dense color=negative"
                            )

        def delete_account(account: Account):
            used_budget = session.query(BudgetItem).filter_by(account_id=account.id).count()
            used_upcoming = session.query(UpcomingExpense).filter_by(account_id=account.id).count()
            used_as_target = (
                session.query(BudgetItem).filter_by(target_account_id=account.id).count()
                + session.query(UpcomingExpense).filter_by(target_account_id=account.id).count()
            )
            used_as_cc_payee = session.query(Account).filter_by(cc_payee_account_id=account.id).count()
            if used_budget or used_upcoming or used_as_target or used_as_cc_payee:
                ui.notify(
                    f"Can't delete '{account.name}' — it's used by {used_budget} budget item(s), "
                    f"{used_upcoming} upcoming expense(s), {used_as_target} transfer(s) targeting it, "
                    f"and {used_as_cc_payee} credit card(s) that pay from it.",
                    type="negative",
                )
                return
            session.delete(account)
            session.commit()
            render_list()

        def open_account_dialog(account: Account | None = None):
            other_accounts = [
                a
                for a in session.query(Account).order_by(Account.name).all()
                if not account or a.id != account.id
            ]
            payee_options = {a.id: a.name for a in other_accounts}

            with ui.dialog() as dialog, ui.card().classes("gap-2 min-w-[420px]"):
                ui.label("Edit Account" if account else "Add Account").classes("text-lg font-bold")
                name_input = ui.input("Name", value=account.name if account else "")
                balance_input = ui.number(
                    "Current Balance", value=account.current_balance if account else 0.0, format="%.2f"
                )
                as_of_input = ui.input(
                    "Balance As Of", value=(account.balance_as_of if account else dt.date.today()).isoformat()
                ).props("type=date")

                threshold_check = ui.checkbox(
                    "Warn when balance drops below",
                    value=bool(account and account.low_balance_threshold is not None),
                )
                threshold_input = ui.number(
                    "Threshold",
                    value=(
                        account.low_balance_threshold if account and account.low_balance_threshold else 0.0
                    ),
                    format="%.2f",
                ).bind_visibility_from(threshold_check, "value")

                growth_check = ui.checkbox(
                    "Earns interest / growth", value=bool(account and account.growth_rate)
                )
                growth_input = ui.number(
                    "Growth Rate (% APY)",
                    value=(account.growth_rate if account and account.growth_rate else 0.0),
                    format="%.2f",
                ).bind_visibility_from(growth_check, "value")

                holdings_input = ui.input(
                    "Holdings (TICKER:weight, TICKER:weight)",
                    value=_format_holdings(account) if account else "",
                )

                cc_check = ui.checkbox(
                    "This is a credit card", value=bool(account and account.is_credit_card)
                )
                with ui.column().bind_visibility_from(cc_check, "value").classes("gap-2 w-full"):
                    cc_payee_select = ui.select(
                        payee_options,
                        label="Payee Account",
                        value=(
                            account.cc_payee_account_id
                            if account and account.cc_payee_account_id in payee_options
                            else None
                        ),
                    )
                    cc_day_input = ui.number(
                        "Direct Debit Day",
                        value=(account.cc_payment_day if account and account.cc_payment_day else 1),
                        min=1,
                        max=31,
                        format="%.0f",
                    )
                    cc_full_check = ui.checkbox(
                        "Pay balance in full each month", value=bool(account and account.cc_pay_in_full)
                    )
                    cc_fixed_input = ui.number(
                        "Fixed Monthly Payment",
                        value=(
                            account.cc_fixed_payment_amount
                            if account and account.cc_fixed_payment_amount
                            else 0.0
                        ),
                        format="%.2f",
                    ).bind_visibility_from(cc_full_check, "value", backward=lambda v: not v)

                def save():
                    name = name_input.value.strip()
                    if not name:
                        ui.notify("Please enter an account name.", type="negative")
                        return
                    existing = session.query(Account).filter(Account.name == name).one_or_none()
                    if existing is not None and existing is not account:
                        ui.notify("An account with this name already exists.", type="negative")
                        return

                    is_cc = cc_check.value
                    if is_cc and cc_payee_select.value is None:
                        ui.notify("Choose a payee account for this credit card.", type="negative")
                        return

                    holdings = _parse_holdings(holdings_input.value)
                    threshold = threshold_input.value if threshold_check.value else None
                    growth_rate = growth_input.value if growth_check.value else None
                    cc_payment_day = int(cc_day_input.value) if is_cc else None
                    cc_pay_in_full = cc_full_check.value if is_cc else False
                    cc_fixed_payment_amount = cc_fixed_input.value if is_cc and not cc_pay_in_full else None

                    obj = account
                    if obj is None:
                        obj = Account(name=name)
                        session.add(obj)

                    obj.name = name
                    obj.current_balance = balance_input.value
                    obj.balance_as_of = dt.date.fromisoformat(as_of_input.value)
                    obj.low_balance_threshold = threshold
                    obj.growth_rate = growth_rate
                    obj.is_credit_card = is_cc
                    obj.cc_payee_account_id = cc_payee_select.value if is_cc else None
                    obj.cc_payment_day = cc_payment_day
                    obj.cc_pay_in_full = cc_pay_in_full
                    obj.cc_fixed_payment_amount = cc_fixed_payment_amount

                    for h in list(obj.holdings):
                        session.delete(h)
                    for ticker, weight in holdings.items():
                        session.add(Holding(account=obj, ticker=ticker, weight=weight))

                    session.commit()
                    dialog.close()
                    render_list()

                with ui.row().classes("justify-end w-full gap-2 mt-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", on_click=save)

            dialog.open()

        render_list()
