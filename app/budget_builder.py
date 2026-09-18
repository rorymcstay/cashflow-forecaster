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

from sqlalchemy.orm import Session

from app.models import OCCURRENCES_PER_YEAR, BudgetItem, FlowType, Frequency, Transaction
from app.seed import get_or_create_category
from app.statement_import import find_matching_budget_item
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


def uncaptured_transactions(session: Session, transactions: list[Transaction]) -> list[Transaction]:
    """Whichever of `transactions` has no covering BudgetItem: neither linked
    at import time (`matched_budget_item`) nor matched live — which also
    picks up items created *after* the transaction was imported, and
    respects vendor-scoped items (a category-level item that only covers
    some vendors leaves the rest of that category's transactions
    uncaptured). This is the complement of what the builder has already
    turned into budget lines: the spend still waiting on one.

    Fetches each distinct account's BudgetItems once and reuses them across
    every transaction on that account, rather than match_budget_item's
    one-query-per-transaction — the difference between a handful of queries
    and thousands when called over a whole transaction pool."""
    items_by_account: dict[int, list[BudgetItem]] = {}
    result = []
    for t in transactions:
        if t.matched_budget_item_id is not None:
            continue
        account_id = t.statement.account_id
        if account_id not in items_by_account:
            items_by_account[account_id] = session.query(BudgetItem).filter_by(account_id=account_id).all()
        if find_matching_budget_item(items_by_account[account_id], t.description, t.date) is not None:
            continue
        result.append(t)
    return result


@dataclass
class UncapturedVendor:
    key: str
    label: str
    transaction_count: int
    total: float  # signed: negative = net expense, positive = net income


def uncaptured_by_vendor(transactions: list[Transaction]) -> list[UncapturedVendor]:
    """uncaptured_transactions(), grouped by merchant and ranked by total
    expense (biggest first) — the shortlist of vendors most worth building a
    budget line for next."""
    groups: dict[str, dict] = {}
    for t in transactions:
        key = merchant_key(t)
        if not key:
            continue
        group = groups.setdefault(key, {"labels": {}, "total": 0.0})
        group["labels"][t.description] = group["labels"].get(t.description, 0) + 1
        group["total"] += t.amount

    rows = [
        UncapturedVendor(
            key=key,
            label=max(group["labels"], key=lambda d: group["labels"][d]),
            transaction_count=sum(group["labels"].values()),
            total=round(group["total"], 2),
        )
        for key, group in groups.items()
    ]
    rows.sort(key=lambda r: r.total)  # most negative (biggest expense) first
    return rows


@dataclass
class StagedLine:
    """A budget-line draft staged in the interactive builder before being
    committed to the database — lets you build up several lines, review or
    discard any of them, and save them all together."""

    description: str
    amount: float
    flow_type: FlowType
    frequency: Frequency
    account_id: int
    category_name: str
    vendor_keys: list[str] = field(default_factory=list)
    vendor_label: str = ""
    effective_from: dt.date = field(default_factory=dt.date.today)
    window_start: dt.date | None = None
    window_end: dt.date | None = None


def commit_staged_line(session: Session, staged: StagedLine) -> BudgetItem:
    """Persist one staged draft as a real BudgetItem. Caller commits the
    session once after committing every staged line in a batch."""
    item = BudgetItem(
        description=staged.description,
        amount=abs(staged.amount),
        flow_type=staged.flow_type,
        frequency=staged.frequency,
        effective_from=staged.effective_from,
        category=get_or_create_category(session, staged.category_name),
        account_id=staged.account_id,
    )
    item.vendor_list = staged.vendor_keys
    session.add(item)
    return item
