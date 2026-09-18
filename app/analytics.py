"""Insights: actual expense spend (with a rolling moving average) versus the
spend implied by matching budget items, bucketed over time and optionally
narrowed by Category and/or Vendor Group — the "forecast vs live" comparison
behind the Insights screen (desktop) / Explorer page (web).

Reuses the same building blocks as the rest of the app rather than
reinventing them: app/forecast.py's generate_occurrences for turning a
recurring BudgetItem into dated forecast events, app/transactions.py's
query_transactions/merchant_key for pulling and grouping real spend.
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.budgets import active_budget_items
from app.forecast import generate_occurrences
from app.models import BudgetItem, FlowType, VendorGroup
from app.transactions import merchant_key, query_transactions

GRANULARITIES = ["Weekly", "Monthly"]


@dataclass
class ExpenseSeries:
    """One point per time bucket spanning [start, end], gap-free (a bucket
    with no matching transactions/occurrences is 0.0, not omitted) so a line
    chart never draws a misleading straight line across a silent stretch."""

    bucket_labels: list[str]
    bucket_starts: list[dt.date]
    actual: list[float]  # positive £ magnitude of matching expenses, per bucket
    actual_moving_avg: list[float]  # trailing moving average of `actual`
    forecast: list[float]  # positive £ magnitude implied by matching budget items, per bucket
    matched_transaction_count: int
    matched_budget_item_ids: list[int]


def _bucket_key(d: dt.date, granularity: str) -> tuple[int, int]:
    if granularity == "Weekly":
        iso = d.isocalendar()
        return (iso[0], iso[1])
    return (d.year, d.month)


def _bucket_label(key: tuple[int, int], granularity: str) -> str:
    if granularity == "Weekly":
        year, week = key
        return f"{year}-W{week:02d}"
    year, month = key
    return dt.date(year, month, 1).strftime("%b %Y")


def _bucket_start(key: tuple[int, int], granularity: str) -> dt.date:
    if granularity == "Weekly":
        year, week = key
        return dt.date.fromisocalendar(year, week, 1)
    year, month = key
    return dt.date(year, month, 1)


def _bucket_range(start: dt.date, end: dt.date, granularity: str) -> list[tuple[int, int]]:
    """Ordered, gap-free bucket keys spanning [start, end] inclusive."""
    keys: list[tuple[int, int]] = []
    cursor = start
    while cursor <= end:
        key = _bucket_key(cursor, granularity)
        if not keys or keys[-1] != key:
            keys.append(key)
        cursor += dt.timedelta(days=1)
    return keys


def _rolling_mean(values: list[float], window: int) -> list[float]:
    """Trailing moving average with a growing window at the start (so the
    line has a value from the very first bucket) rather than leaving early
    points undefined."""
    window = max(window, 1)
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1) : i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def expense_vs_forecast(
    session: Session,
    granularity: str,
    start: dt.date,
    end: dt.date,
    ma_window: int = 3,
    category_id: int | None = None,
    vendor_group_id: int | None = None,
    account_ids: list[int] | None = None,
) -> ExpenseSeries:
    """Actual expense spend vs matching-budget-item forecast, bucketed by
    `granularity` ("Weekly"/"Monthly") over [start, end].

    `category_id`/`vendor_group_id` narrow which transactions count as
    "matching" (both may be set together, same combined-filter idiom as
    app/budget_builder.py's vendor+category aggregation); leaving both unset
    covers every expense. A vendor group only ever matches a BudgetItem that
    explicitly scopes itself to overlapping vendors (`BudgetItem.vendors`) —
    an unscoped, whole-category item can't be attributed to one vendor group
    without guessing, so it's left out of that forecast rather than
    double-counted or wrongly excluded from every other group's.
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"Unknown granularity {granularity!r} — expected one of {GRANULARITIES}")

    vendor_keys: set[str] | None = None
    if vendor_group_id is not None:
        group = session.get(VendorGroup, vendor_group_id)
        vendor_keys = set(group.vendor_list) if group else set()

    transactions = query_transactions(
        session,
        account_ids=account_ids,
        category_ids=[category_id] if category_id is not None else None,
        start_date=start,
        end_date=end,
    )
    expenses = [t for t in transactions if t.amount < 0]
    if vendor_keys is not None:
        expenses = [t for t in expenses if merchant_key(t) in vendor_keys]

    buckets = _bucket_range(start, end, granularity)
    actual_by_bucket: dict[tuple[int, int], float] = dict.fromkeys(buckets, 0.0)
    for t in expenses:
        actual_by_bucket[_bucket_key(t.date, granularity)] += -t.amount

    items_query = active_budget_items(session).filter(BudgetItem.flow_type == FlowType.EXPENSE)
    if category_id is not None:
        items_query = items_query.filter(BudgetItem.category_id == category_id)
    items = items_query.all()
    if vendor_keys is not None:
        items = [i for i in items if vendor_keys & set(i.vendor_list)]

    forecast_by_bucket: dict[tuple[int, int], float] = dict.fromkeys(buckets, 0.0)
    for item in items:
        for occ in generate_occurrences(
            item.effective_from, item.effective_until, item.frequency, start, end
        ):
            forecast_by_bucket[_bucket_key(occ, granularity)] += item.amount

    actual = [round(actual_by_bucket[b], 2) for b in buckets]
    forecast = [round(forecast_by_bucket[b], 2) for b in buckets]

    return ExpenseSeries(
        bucket_labels=[_bucket_label(b, granularity) for b in buckets],
        bucket_starts=[_bucket_start(b, granularity) for b in buckets],
        actual=actual,
        actual_moving_avg=[round(v, 2) for v in _rolling_mean(actual, ma_window)],
        forecast=forecast,
        matched_transaction_count=len(expenses),
        matched_budget_item_ids=[i.id for i in items],
    )
