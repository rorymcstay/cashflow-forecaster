"""Cross-statement transaction queries for the Transactions tab: filtering
across every account, merchant-based similarity, and recurring-group lookup.

Reuses app/statements.py's normalize_description/find_recurring_transactions
(the same merchant-grouping logic that already seeds budget suggestions in
app/statement_import.py) rather than introducing a second definition of what
counts as "recurring" or "the same merchant".
"""

import calendar
import datetime as dt
from collections import defaultdict

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Statement, Transaction
from app.statements import find_recurring_transactions, normalize_description

UNCATEGORIZED = "Uncategorized"

# No real Category row ever has this id (autoincrement starts at 1) — used as
# a pseudo-category id so the category filter can include/exclude
# transactions with no category at all, not just named ones.
UNCATEGORIZED_ID = -1

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
    loaded into it in the first place.

    `category_ids` may include UNCATEGORIZED_ID to match transactions with no
    category at all, alongside or instead of real category ids."""
    query = session.query(Transaction).join(Statement)
    if account_ids:
        query = query.filter(Statement.account_id.in_(account_ids))
    if category_ids:
        include_uncategorized = UNCATEGORIZED_ID in category_ids
        real_ids = [c for c in category_ids if c != UNCATEGORIZED_ID]
        if include_uncategorized and real_ids:
            query = query.filter(
                or_(Transaction.category_id.in_(real_ids), Transaction.category_id.is_(None))
            )
        elif include_uncategorized:
            query = query.filter(Transaction.category_id.is_(None))
        else:
            query = query.filter(Transaction.category_id.in_(real_ids))
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


def spend_breakdown(transactions: list[Transaction], top_merchants: int = 10) -> dict:
    """Spend-analysis summary over exactly the given transactions — callers
    pass whatever's currently filtered/displayed (by account, date range,
    etc.) so the summary always matches what's on screen.

    Category and merchant breakdowns only consider expenses (amount < 0),
    reported as positive magnitudes, since "where did the money go" is the
    usual question; income is only used for the income/net totals and the
    monthly trend."""
    total_income = sum(t.amount for t in transactions if t.amount > 0)
    total_expense = -sum(t.amount for t in transactions if t.amount < 0)

    category_totals: dict[str, float] = defaultdict(float)
    merchant_totals: dict[str, dict] = defaultdict(lambda: {"description": "", "count": 0, "amount": 0.0})
    month_totals: dict[str, dict] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})

    for t in transactions:
        month_key = t.date.strftime("%Y-%m")
        if t.amount > 0:
            month_totals[month_key]["income"] += t.amount
            continue
        amount = -t.amount
        month_totals[month_key]["expense"] += amount
        category_name = t.category.name if t.category else UNCATEGORIZED
        category_totals[category_name] += amount
        key = merchant_key(t)
        if key:
            entry = merchant_totals[key]
            entry["description"] = t.description
            entry["count"] += 1
            entry["amount"] += amount

    by_category = sorted(
        (
            {
                "category": name,
                "amount": round(amount, 2),
                "pct": round(amount / total_expense * 100, 1) if total_expense else 0.0,
            }
            for name, amount in category_totals.items()
        ),
        key=lambda r: r["amount"],
        reverse=True,
    )

    top_merchant_rows = sorted(
        (
            {"merchant": v["description"], "count": v["count"], "amount": round(v["amount"], 2)}
            for v in merchant_totals.values()
        ),
        key=lambda r: r["amount"],
        reverse=True,
    )[:top_merchants]

    by_month = [
        {
            "month": key,
            "month_label": dt.date.fromisoformat(f"{key}-01").strftime("%b %Y"),
            "income": round(v["income"], 2),
            "expense": round(v["expense"], 2),
            "net": round(v["income"] - v["expense"], 2),
        }
        for key, v in sorted(month_totals.items())
    ]

    months_spanned = len(month_totals) or 1

    return {
        "transaction_count": len(transactions),
        "total_income": round(total_income, 2),
        "total_expense": round(total_expense, 2),
        "net": round(total_income - total_expense, 2),
        "months_spanned": months_spanned,
        "avg_monthly_income": round(total_income / months_spanned, 2),
        "avg_monthly_expense": round(total_expense / months_spanned, 2),
        "by_category": by_category,
        "top_merchants": top_merchant_rows,
        "by_month": by_month,
    }
