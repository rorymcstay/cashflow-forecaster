"""Cross-statement transaction queries for the Transactions tab: filtering
across every account, merchant-based similarity, and recurring-group lookup.

Reuses app/statements.py's normalize_description/find_recurring_transactions
(the same merchant-grouping logic that already seeds budget suggestions in
app/statement_import.py) rather than introducing a second definition of what
counts as "recurring" or "the same merchant".
"""

import calendar
import datetime as dt

from sqlalchemy.orm import Session

from app.models import Statement, Transaction
from app.statements import find_recurring_transactions, normalize_description

# Quick date-range filters offered on the Transactions page (web + desktop),
# in display order. "all"/"custom" are handled specially by
# date_range_preset() rather than listed here as a fixed offset.
DATE_RANGE_PRESETS: dict[str, str] = {
    "all": "All time",
    "this_month": "This month",
    "last_30": "Last 30 days",
    "last_3m": "Last 3 months",
    "last_6m": "Last 6 months",
    "ytd": "Year to date",
    "custom": "Custom range",
}


def _months_ago(d: dt.date, months: int) -> dt.date:
    total = d.year * 12 + (d.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def date_range_preset(key: str, today: dt.date | None = None) -> tuple[dt.date | None, dt.date | None]:
    """(start, end) for a named DATE_RANGE_PRESETS key, end always `today`.
    Returns (None, None) for "all", "custom", or an unknown key — both mean
    "no preset-derived bound", since "custom" defers to whatever dates the
    user has picked directly."""
    today = today or dt.date.today()
    if key == "this_month":
        return dt.date(today.year, today.month, 1), today
    if key == "last_30":
        return today - dt.timedelta(days=30), today
    if key == "last_3m":
        return _months_ago(today, 3), today
    if key == "last_6m":
        return _months_ago(today, 6), today
    if key == "ytd":
        return dt.date(today.year, 1, 1), today
    return None, None


def query_transactions(
    session: Session,
    account_ids: list[int] | None = None,
    category_ids: list[int] | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> list[Transaction]:
    """Transactions across every statement, optionally narrowed by account,
    category, or date range. Free-text search and column-level filtering are
    left to the table widget client-side — this only narrows what gets
    loaded into it in the first place."""
    query = session.query(Transaction).join(Statement)
    if account_ids:
        query = query.filter(Statement.account_id.in_(account_ids))
    if category_ids:
        query = query.filter(Transaction.category_id.in_(category_ids))
    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)
    return query.order_by(Transaction.date.desc()).all()


def merchant_key(transaction: Transaction) -> str:
    """The normalised merchant name used to group a transaction with others
    that likely represent the same payee."""
    return normalize_description(transaction.description)


def recurring_groups_by_merchant(transactions: list[Transaction]) -> dict[str, dict]:
    """merchant-key -> recurring-group stats (occurrences, avg amount, guessed
    frequency, direction) for every merchant in `transactions` that qualifies
    as recurring, per the same detector used to seed budget suggestions."""
    tx_dicts = [
        {"date": t.date.isoformat(), "description": t.description, "amount": t.amount} for t in transactions
    ]
    groups = find_recurring_transactions(tx_dicts)
    return {g["description"]: g for g in groups}


def similar_transactions(
    transaction: Transaction, pool: list[Transaction], limit: int = 25
) -> list[Transaction]:
    """Every other transaction in `pool` sharing this one's normalised
    merchant name, most recent first — powers both the "explore by clicking"
    list in the detail pane and the recurring-group badge in the table."""
    key = merchant_key(transaction)
    if not key:
        return []
    matches = [t for t in pool if t.id != transaction.id and merchant_key(t) == key]
    matches.sort(key=lambda t: t.date, reverse=True)
    return matches[:limit]
