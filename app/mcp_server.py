import calendar
import datetime as dt

from mcp.server.mcpserver import MCPServer

from app.db import get_session, init_db
from app.forecast import (
    account_daily_forecast, combined_daily_forecast, low_balance_warnings, monthly_budget_summary,
)
from app.models import Account, BudgetItem, Category, FlowType, Frequency, UpcomingExpense
from app.seed import get_or_create_category, seed_defaults
from app.statements import (
    extract_csv_transactions, find_recurring_transactions as _find_recurring, read_pdf_text as _read_pdf_text,
)

server = MCPServer(
    "household-budgeting",
    instructions=(
        "Tools for the household budgeting app: analysing bank/card statements, "
        "and reading/writing the same SQLite data the desktop GUI uses (accounts, "
        "recurring budget items, one-off upcoming expenses, and cashflow forecasts). "
        "Money amounts on committed budget items and upcoming expenses are always "
        "positive; direction (income vs expense) is a separate field."
    ),
)

init_db()
_seed_session = get_session()
seed_defaults(_seed_session)
_seed_session.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _account_to_dict(a: Account) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "current_balance": a.current_balance,
        "balance_as_of": a.balance_as_of.isoformat(),
        "low_balance_threshold": a.low_balance_threshold,
    }


def _budget_item_to_dict(b: BudgetItem) -> dict:
    return {
        "id": b.id,
        "description": b.description,
        "amount": b.amount,
        "flow_type": b.flow_type.value,
        "frequency": b.frequency.value,
        "effective_from": b.effective_from.isoformat(),
        "effective_until": b.effective_until.isoformat() if b.effective_until else None,
        "category": b.category.name,
        "account": b.account.name,
        "notes": b.notes,
    }


def _upcoming_to_dict(u: UpcomingExpense) -> dict:
    return {
        "id": u.id,
        "date": u.date.isoformat(),
        "description": u.description,
        "amount": u.amount,
        "category": u.category.name,
        "account": u.account.name,
    }


def _resolve_account(session, name: str) -> Account:
    account = session.query(Account).filter(Account.name == name).one_or_none()
    if account is None:
        names = [a.name for a in session.query(Account).order_by(Account.name).all()]
        raise ValueError(f"No account named '{name}'. Existing accounts: {names}. Use create_account first.")
    return account


def _resolve_frequency(value: str) -> Frequency:
    for f in Frequency:
        if f.value.lower() == value.lower() or f.name.lower() == value.lower():
            return f
    raise ValueError(f"Unknown frequency '{value}'. Valid values: {[f.value for f in Frequency]}")


def _resolve_flow_type(value: str) -> FlowType:
    for ft in FlowType:
        if ft.value.lower() == value.lower() or ft.name.lower() == value.lower():
            return ft
    raise ValueError(f"Unknown type '{value}'. Valid values: {[ft.value for ft in FlowType]}")


# ---------------------------------------------------------------------------
# statement analysis
# ---------------------------------------------------------------------------

@server.tool()
def read_pdf_statement(file_path: str, first_page: int = 1, last_page: int | None = None) -> str:
    """Extract raw text from a PDF bank/card statement, page by page.

    Use this for statements that aren't a clean CSV (e.g. HSBC, Amex). Bank
    PDF layouts vary too much for one hardcoded table parser, so this hands
    back readable text for you to interpret directly — read it, transcribe
    the transactions you find (date, description, signed amount) into a
    list, then optionally pass that list to find_recurring_transactions.
    For long statements, page through with first_page/last_page.
    """
    return _read_pdf_text(file_path, first_page, last_page)


@server.tool()
def extract_csv_statement(file_path: str) -> list[dict]:
    """Parse a bank/card CSV export into transactions.

    Returns a list of {date, description, amount, category_hint,
    source_format}, where amount is signed (positive = money in, negative =
    money out). Recognises the Monzo export format specifically; otherwise
    sniffs Date/Description/Amount-style columns, which covers most UK bank
    CSV exports.
    """
    return extract_csv_transactions(file_path)


@server.tool()
def find_recurring_transactions(transactions: list[dict], min_occurrences: int = 2,
                                 amount_tolerance_pct: float = 0.15) -> list[dict]:
    """Group transactions by normalised merchant name and flag recurring-bill candidates.

    Each transaction dict needs `date` (ISO string), `description`, and
    `amount` (signed). Feed it the output of extract_csv_statement, or a
    list you assembled yourself after reading a PDF with read_pdf_statement.
    Returns, per merchant: occurrence count, first/last date, avg/min/max
    amount, the most common day of month, and a guessed frequency — the
    same kind of analysis used to cross-check this budget's recurring bills
    in the first place.
    """
    return _find_recurring(transactions, min_occurrences, amount_tolerance_pct)


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------

@server.tool()
def list_accounts() -> list[dict]:
    """List all accounts with their current balance, balance-as-of date, and low-balance threshold."""
    session = get_session()
    try:
        return [_account_to_dict(a) for a in session.query(Account).order_by(Account.name).all()]
    finally:
        session.close()


@server.tool()
def create_account(name: str, current_balance: float = 0.0, balance_as_of: str | None = None,
                    low_balance_threshold: float | None = None) -> dict:
    """Create a new account. balance_as_of is an ISO date string (e.g. '2026-08-02'), defaults to today."""
    session = get_session()
    try:
        if session.query(Account).filter_by(name=name).one_or_none() is not None:
            raise ValueError(f"An account named '{name}' already exists.")
        account = Account(
            name=name,
            current_balance=current_balance,
            balance_as_of=_parse_date(balance_as_of) or dt.date.today(),
            low_balance_threshold=low_balance_threshold,
        )
        session.add(account)
        session.commit()
        return _account_to_dict(account)
    finally:
        session.close()


@server.tool()
def update_account(account_id: int, name: str | None = None, current_balance: float | None = None,
                    balance_as_of: str | None = None, low_balance_threshold: float | None = None,
                    clear_threshold: bool = False) -> dict:
    """Update an account. Only pass the fields you want to change; set clear_threshold=True to remove a warning threshold."""
    session = get_session()
    try:
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError(f"No account with id {account_id}")
        if name is not None:
            account.name = name
        if current_balance is not None:
            account.current_balance = current_balance
        if balance_as_of is not None:
            account.balance_as_of = _parse_date(balance_as_of)
        if clear_threshold:
            account.low_balance_threshold = None
        elif low_balance_threshold is not None:
            account.low_balance_threshold = low_balance_threshold
        session.commit()
        return _account_to_dict(account)
    finally:
        session.close()


@server.tool()
def delete_account(account_id: int) -> dict:
    """Delete an account. Refused if any budget item or upcoming expense still references it."""
    session = get_session()
    try:
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError(f"No account with id {account_id}")
        used_budget = session.query(BudgetItem).filter_by(account_id=account_id).count()
        used_upcoming = session.query(UpcomingExpense).filter_by(account_id=account_id).count()
        if used_budget or used_upcoming:
            raise ValueError(
                f"Can't delete '{account.name}' — used by {used_budget} budget item(s) and "
                f"{used_upcoming} upcoming expense(s). Reassign or delete those first."
            )
        name = account.name
        session.delete(account)
        session.commit()
        return {"deleted": name}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------

@server.tool()
def list_categories() -> list[str]:
    """List all category names used to tag budget items and upcoming expenses."""
    session = get_session()
    try:
        return [c.name for c in session.query(Category).order_by(Category.name).all()]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# budget items
# ---------------------------------------------------------------------------

@server.tool()
def list_budget_items(active_only: bool = False, as_of: str | None = None) -> list[dict]:
    """List committed recurring payments (income or expense).

    If active_only is True, only include items whose effective_from/
    effective_until window covers as_of (defaults to today).
    """
    session = get_session()
    try:
        items = session.query(BudgetItem).order_by(BudgetItem.effective_from).all()
        if active_only:
            ref = _parse_date(as_of) or dt.date.today()
            items = [i for i in items if i.is_active_on(ref)]
        return [_budget_item_to_dict(i) for i in items]
    finally:
        session.close()


@server.tool()
def create_budget_item(description: str, amount: float, category: str, account: str,
                        flow_type: str = "Expense", frequency: str = "Monthly",
                        effective_from: str | None = None, effective_until: str | None = None,
                        notes: str | None = None) -> dict:
    """Add a committed recurring payment.

    flow_type: 'Income' or 'Expense'. frequency: Weekly, Fortnightly,
    4-Weekly, Monthly, Quarterly, 6-Monthly, or Annually. effective_from
    (ISO date, defaults to today) anchors the recurrence — e.g. for Monthly
    its day-of-month is the day the payment recurs on. Category is created
    automatically if it doesn't exist; account must already exist (see
    list_accounts / create_account).
    """
    session = get_session()
    try:
        acc = _resolve_account(session, account)
        item = BudgetItem(
            description=description,
            amount=amount,
            flow_type=_resolve_flow_type(flow_type),
            frequency=_resolve_frequency(frequency),
            effective_from=_parse_date(effective_from) or dt.date.today(),
            effective_until=_parse_date(effective_until),
            notes=notes,
            category=get_or_create_category(session, category),
            account=acc,
        )
        session.add(item)
        session.commit()
        return _budget_item_to_dict(item)
    finally:
        session.close()


@server.tool()
def update_budget_item(item_id: int, description: str | None = None, amount: float | None = None,
                        category: str | None = None, account: str | None = None,
                        flow_type: str | None = None, frequency: str | None = None,
                        effective_from: str | None = None, effective_until: str | None = None,
                        clear_effective_until: bool = False, notes: str | None = None) -> dict:
    """Update a budget item. Only pass the fields you want to change."""
    session = get_session()
    try:
        item = session.get(BudgetItem, item_id)
        if item is None:
            raise ValueError(f"No budget item with id {item_id}")
        if description is not None:
            item.description = description
        if amount is not None:
            item.amount = amount
        if category is not None:
            item.category = get_or_create_category(session, category)
        if account is not None:
            item.account = _resolve_account(session, account)
        if flow_type is not None:
            item.flow_type = _resolve_flow_type(flow_type)
        if frequency is not None:
            item.frequency = _resolve_frequency(frequency)
        if effective_from is not None:
            item.effective_from = _parse_date(effective_from)
        if clear_effective_until:
            item.effective_until = None
        elif effective_until is not None:
            item.effective_until = _parse_date(effective_until)
        if notes is not None:
            item.notes = notes
        session.commit()
        return _budget_item_to_dict(item)
    finally:
        session.close()


@server.tool()
def delete_budget_item(item_id: int) -> dict:
    """Delete a budget item."""
    session = get_session()
    try:
        item = session.get(BudgetItem, item_id)
        if item is None:
            raise ValueError(f"No budget item with id {item_id}")
        desc = item.description
        session.delete(item)
        session.commit()
        return {"deleted": desc}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# upcoming expenses
# ---------------------------------------------------------------------------

@server.tool()
def list_upcoming_expenses(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """List one-off upcoming expenses, optionally filtered to a date range (ISO dates)."""
    session = get_session()
    try:
        query = session.query(UpcomingExpense)
        if start_date:
            query = query.filter(UpcomingExpense.date >= _parse_date(start_date))
        if end_date:
            query = query.filter(UpcomingExpense.date <= _parse_date(end_date))
        return [_upcoming_to_dict(u) for u in query.order_by(UpcomingExpense.date).all()]
    finally:
        session.close()


@server.tool()
def create_upcoming_expense(date: str, description: str, amount: float, category: str, account: str) -> dict:
    """Add a one-off dated expense (ISO date string).

    Category is created automatically if new; account must already exist.
    """
    session = get_session()
    try:
        acc = _resolve_account(session, account)
        expense = UpcomingExpense(
            date=_parse_date(date),
            description=description,
            amount=amount,
            category=get_or_create_category(session, category),
            account=acc,
        )
        session.add(expense)
        session.commit()
        return _upcoming_to_dict(expense)
    finally:
        session.close()


@server.tool()
def update_upcoming_expense(item_id: int, date: str | None = None, description: str | None = None,
                             amount: float | None = None, category: str | None = None,
                             account: str | None = None) -> dict:
    """Update a one-off upcoming expense. Only pass the fields you want to change."""
    session = get_session()
    try:
        expense = session.get(UpcomingExpense, item_id)
        if expense is None:
            raise ValueError(f"No upcoming expense with id {item_id}")
        if date is not None:
            expense.date = _parse_date(date)
        if description is not None:
            expense.description = description
        if amount is not None:
            expense.amount = amount
        if category is not None:
            expense.category = get_or_create_category(session, category)
        if account is not None:
            expense.account = _resolve_account(session, account)
        session.commit()
        return _upcoming_to_dict(expense)
    finally:
        session.close()


@server.tool()
def delete_upcoming_expense(item_id: int) -> dict:
    """Delete a one-off upcoming expense."""
    session = get_session()
    try:
        expense = session.get(UpcomingExpense, item_id)
        if expense is None:
            raise ValueError(f"No upcoming expense with id {item_id}")
        desc = expense.description
        session.delete(expense)
        session.commit()
        return {"deleted": desc}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# budget summary + cashflow forecast
# ---------------------------------------------------------------------------

@server.tool()
def get_budget_summary(as_of: str | None = None) -> dict:
    """Monthly-equivalent income/expense totals by category.

    Only includes budget items active on `as_of` (defaults to today). Every
    frequency is normalised to a per-month figure (e.g. a weekly item counts
    as amount * 52/12).
    """
    session = get_session()
    try:
        ref = _parse_date(as_of) or dt.date.today()
        rows = monthly_budget_summary(session, ref).to_dicts()
        total_income = sum(r["monthly_amount"] for r in rows if r["flow_type"] == "Income")
        total_expense = sum(r["monthly_amount"] for r in rows if r["flow_type"] == "Expense")
        return {
            "as_of": ref.isoformat(),
            "by_category": rows,
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "net_remaining": round(total_income - total_expense, 2),
        }
    finally:
        session.close()


@server.tool()
def get_cashflow_forecast(start_date: str, end_date: str | None = None, account: str | None = None) -> dict:
    """Daily cashflow forecast over a date range (any length — a week, several
    months, a year).

    start_date and end_date are ISO dates ('YYYY-MM-DD'). As a shortcut,
    start_date may instead be a month ('YYYY-MM') with end_date omitted, in
    which case the forecast covers that whole calendar month.

    If account is omitted, returns the combined forecast across every
    account (valid from the latest balance_as_of date among them, with a
    per-account balance breakdown too). Otherwise returns the forecast for
    that single account, starting from its current_balance / balance_as_of,
    including which days (if any) breach its low-balance threshold.
    """
    session = get_session()
    try:
        if end_date is None and len(start_date) == 7:
            year, month_num = (int(p) for p in start_date.split("-"))
            range_start = dt.date(year, month_num, 1)
            range_end = dt.date(year, month_num, calendar.monthrange(year, month_num)[1])
        else:
            range_start = _parse_date(start_date)
            range_end = _parse_date(end_date) if end_date else range_start

        if account is None:
            df = combined_daily_forecast(session, range_start, range_end)
            acc = None
        else:
            acc = _resolve_account(session, account)
            df = account_daily_forecast(session, acc, range_start, range_end)

        if df.height == 0:
            return {
                "start_date": range_start.isoformat(),
                "end_date": range_end.isoformat(),
                "account": account,
                "days": [],
                "note": "No data — the account's balance-as-of date is after this range.",
            }

        days = df.to_dicts()
        for day in days:
            day["date"] = day["date"].isoformat()

        warnings = []
        if acc is not None:
            warnings = [
                {**w, "date": w["date"].isoformat()}
                for w in low_balance_warnings(session, range_start, range_end)
                if w["account"] == acc.name
            ]

        return {
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
            "account": account or "All Accounts (combined)",
            "days": days,
            "closing_balance": days[-1]["balance"],
            "warnings": warnings,
        }
    finally:
        session.close()


@server.tool()
def get_low_balance_warnings(start_date: str, end_date: str) -> list[dict]:
    """Every (account, date) in the given range where the forecast balance dips below that account's threshold."""
    session = get_session()
    try:
        warnings = low_balance_warnings(session, _parse_date(start_date), _parse_date(end_date))
        return [{**w, "date": w["date"].isoformat()} for w in warnings]
    finally:
        session.close()


def main():
    server.run()


if __name__ == "__main__":
    main()
