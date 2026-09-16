"""Suggested Budget Builder: turn a slice of transaction history — filtered
by vendor and/or category, across one or more source accounts — into a
per-period spending average, which can then be committed as a BudgetItem on
any target account (not necessarily one of the source accounts — e.g.
"Holiday" spend averaged across every account, budgeted onto one card).

This is the deliberate, user-directed counterpart to
app/statement_import.py's automatic generate_suggestions: you pick the group
yourself (vendor, category, accounts, interval) rather than waiting for the
recurring-transaction detector to spot a 1:1 merchant match.
"""

import datetime as dt
from dataclasses import dataclass, field

from app.models import OCCURRENCES_PER_YEAR, Frequency, Transaction
from app.transactions import merchant_key

DAYS_PER_YEAR = 365.25


@dataclass
class VendorOption:
    key: str
    label: str
    transaction_count: int


def vendor_options(transactions: list[Transaction]) -> list[VendorOption]:
    """Distinct merchants across `transactions`, using each one's most
    common raw description as the display label — so the picker shows
    "UBER *TRIP" rather than the normalised grouping key — sorted by how
    often they occur (the vendors worth building a budget line for are
    usually the most frequent ones)."""
    labels_by_key: dict[str, dict[str, int]] = {}
    for t in transactions:
        key = merchant_key(t)
        if not key:
            continue
        labels = labels_by_key.setdefault(key, {})
        labels[t.description] = labels.get(t.description, 0) + 1

    options = [
        VendorOption(
            key=key, label=max(labels, key=lambda d: labels[d]), transaction_count=sum(labels.values())
        )
        for key, labels in labels_by_key.items()
    ]
    options.sort(key=lambda o: o.transaction_count, reverse=True)
    return options


@dataclass
class SpendAggregate:
    transactions: list[Transaction] = field(default_factory=list)
    total: float = 0.0  # signed: negative = net expense, positive = net income
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    periods: float = 0.0  # number of `interval`-sized periods spanned by [start_date, end_date]
    average_per_period: float = 0.0  # signed; total / periods


def aggregate_spend(
    transactions: list[Transaction],
    interval: Frequency,
    vendor_keys: list[str] | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> SpendAggregate:
    """Sum + per-period average for whichever of `transactions` matches any
    of `vendor_keys` (if given — one or more vendors are grouped together
    into a single average, e.g. "Uber" + "Uber Eats" as one line). Callers
    are expected to have already narrowed `transactions` by
    account/category/date via query_transactions — this only adds the
    vendor-by-merchant filter, which isn't a DB column.

    The averaging window defaults to the span between the matched
    transactions' own dates, but an explicit [start_date, end_date] (e.g. the
    same bounds already passed to query_transactions) is used instead when
    given — a sparse or seasonal vendor (an annual holiday, say) should
    dilute across the *requested* lookback, not just the days it happened to
    appear on, so a short apparent history doesn't overstate the average.
    """
    vendor_key_set = set(vendor_keys) if vendor_keys else None
    matched = [t for t in transactions if vendor_key_set is None or merchant_key(t) in vendor_key_set]
    matched.sort(key=lambda t: t.date, reverse=True)

    total = round(sum(t.amount for t in matched), 2)
    dates = [t.date for t in matched]
    window_start = start_date or (min(dates) if dates else None)
    window_end = end_date or (max(dates) if dates else None)

    periods = 0.0
    if window_start and window_end:
        years_spanned = max((window_end - window_start).days, 1) / DAYS_PER_YEAR
        periods = years_spanned * OCCURRENCES_PER_YEAR[interval]

    average_per_period = round(total / periods, 2) if periods else 0.0

    return SpendAggregate(
        transactions=matched,
        total=total,
        start_date=window_start,
        end_date=window_end,
        periods=round(periods, 2),
        average_per_period=average_per_period,
    )
