"""Vendor group suggestion engine: proposes candidate app.models.VendorGroup
groupings from transaction history, for review on the Vendor Groups screen
before creating anything — the same "recurring-detection primes a draft you
can edit or discard" shape as app/budget_recommender.py, applied to grouping
vendors instead of budgeting them.

Rule: recurring vendors (find_recurring_transactions — the same detector
used everywhere else) that aren't already covered by any existing vendor
group are bucketed by their classified category (app/classify.py); any
category with at least MIN_VENDORS such vendors is proposed as a group
(e.g. "Groceries": TESCO, ALDI, WAITROSE) — a real starting point for the
cases vendor groups exist for: reporting on a set of merchants as one unit
regardless of which individual category each transaction lands in.
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.budget_builder import vendor_options
from app.classify import classify
from app.statement_import import UNCATEGORIZED
from app.statements import find_recurring_transactions
from app.transactions import query_transactions
from app.vendor_groups import list_vendor_groups

MIN_VENDORS = 2
MIN_OCCURRENCES = 2
DEFAULT_LOOKBACK_DAYS = 365


@dataclass
class VendorGroupSuggestion:
    name: str
    vendor_keys: list[str]
    vendor_labels: list[str] = field(default_factory=list)
    rationale: str = ""


def recommend_vendor_groups(
    session: Session,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_vendors: int = MIN_VENDORS,
    account_ids: list[int] | None = None,
) -> list[VendorGroupSuggestion]:
    end = dt.date.today()
    start = end - dt.timedelta(days=lookback_days)

    transactions = query_transactions(session, account_ids=account_ids, start_date=start, end_date=end)
    expenses = [t for t in transactions if t.amount < 0]
    if not expenses:
        return []

    already_grouped = {key for g in list_vendor_groups(session) for key in g.vendor_list}
    labels_by_key = {v.key: v.label for v in vendor_options(expenses)}

    tx_dicts = [
        {"date": t.date.isoformat(), "description": t.description, "amount": t.amount} for t in expenses
    ]
    groups = find_recurring_transactions(tx_dicts, min_occurrences=MIN_OCCURRENCES)

    keys_by_category: dict[str, list[str]] = defaultdict(list)
    for group in groups:
        if group["direction"] != "out":
            continue
        key = group["description"]
        if key in already_grouped:
            continue
        category_name = classify(group["sample_descriptions"][0]) or UNCATEGORIZED
        if category_name == UNCATEGORIZED:
            continue
        keys_by_category[category_name].append(key)

    suggestions = []
    for category_name, keys in keys_by_category.items():
        if len(keys) < min_vendors:
            continue
        labels = [labels_by_key.get(k, k) for k in keys]
        suggestions.append(
            VendorGroupSuggestion(
                name=category_name,
                vendor_keys=keys,
                vendor_labels=labels,
                rationale=(
                    f"{len(keys)} recurring vendors classified as {category_name}, not yet in any "
                    f"vendor group: {', '.join(labels)}."
                ),
            )
        )
    suggestions.sort(key=lambda s: -len(s.vendor_keys))
    return suggestions
