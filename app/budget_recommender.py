"""Rule-based budget recommender: proposes a full slate of StagedLine drafts
from transaction history, primed straight into the Budget Builder's staging
area (app/budget_builder.py) for review before saving — an automated
counterpart to manually building each line by hand.

Per account, the rules form a small decision tree, driven by the same
recurring-transaction detector used elsewhere (app/statements.py) and the
same moving-average machinery behind the Insights screen (app/analytics.py)
rather than reinventing either:

    uncaptured expenses (not already matched to an active-budget item)
      -> grouped into recurring vendor candidates (find_recurring_transactions)
           tight amount spread (<= TIGHT_SPREAD)?
             yes -> "recurring_bill" (high confidence): fixed amount at the
                    detector's own guessed frequency, vendor-scoped
           spread still within LOOSE_SPREAD (a usage-based charge — fuel,
           cloud hosting — allowed to vary more)?
             yes -> "recurring_variable" (medium confidence): same shape,
                    average amount
           too irregular to call a vendor-level bill at all
             -> left for the category catch-all below
      -> whatever's left over from non-lifestyle categories, grouped by
         category: take the 3-month trailing moving average of that
         category's monthly spend on this account
         (app.analytics.expense_vs_forecast) and, if it clears
         MIN_MONTHLY_AMOUNT, propose a whole-category monthly line
         ("category_catchall", low confidence)

Before any of the above runs, LIFESTYLE_CATEGORIES (Eating out,
Subscriptions — vendor-name classified, same as everywhere else) are split
out and pooled into one line per category instead: a coffee shop visited
twice and a pub visited once are each too sparse/small to deserve their own
budget line, but collectively they're real, regular spend worth a single
line. A description that looks like a standing order is always exempted
from this pooling and flows through the normal per-vendor path — a standing
order is usually a distinct, deliberate commitment (rent, a loan repayment)
that shouldn't be diluted into a lifestyle-spending average.
"""

import datetime as dt
import re

from sqlalchemy.orm import Session

from app.analytics import expense_vs_forecast
from app.budget_builder import StagedLine, aggregate_spend, uncaptured_transactions, vendor_options
from app.classify import classify
from app.models import Account, Frequency, FlowType
from app.statement_import import UNCATEGORIZED
from app.statements import find_recurring_transactions
from app.transactions import merchant_key, query_transactions

TIGHT_SPREAD = 0.10
LOOSE_SPREAD = 0.40
MIN_OCCURRENCES = 2
MIN_MONTHLY_AMOUNT = 5.0
DEFAULT_LOOKBACK_DAYS = 365

# Small/varied spend worth pooling into one line per category rather than
# one line per vendor — see module docstring. Names match classify.py's
# CATEGORY_KEYWORDS keys exactly, since "looks like a pub/restaurant/coffee
# shop" or "looks like a subscription" is itself just name-based
# classification, and reusing it keeps one definition of what those mean
# rather than a second keyword bank drifting out of sync with the first.
LIFESTYLE_CATEGORIES = {"Eating out", "Subscriptions"}

_STANDING_ORDER_PATTERN = re.compile(r"STANDING\s*ORDER|\bSTO\b|\bS/O\b", re.IGNORECASE)

_FREQUENCY_BY_LABEL = {f.value: f for f in Frequency}


def _looks_like_standing_order(description: str) -> bool:
    return _STANDING_ORDER_PATTERN.search(description) is not None


def _split_lifestyle_transactions(expenses: list) -> tuple[list, list]:
    """(lifestyle, individual) — lifestyle holds whatever classifies into
    LIFESTYLE_CATEGORIES and doesn't look like a standing order; everything
    else (including standing-order-looking lifestyle-category transactions)
    stays in `individual` for the normal per-vendor rules."""
    lifestyle, individual = [], []
    for t in expenses:
        if not _looks_like_standing_order(t.description) and classify(t.description) in LIFESTYLE_CATEGORIES:
            lifestyle.append(t)
        else:
            individual.append(t)
    return lifestyle, individual


def _lifestyle_pool_lines(lifestyle_txs: list, account: Account) -> list[StagedLine]:
    if not lifestyle_txs:
        return []
    labels_by_key = {v.key: v.label for v in vendor_options(lifestyle_txs)}

    by_category: dict[str, list] = {}
    for t in lifestyle_txs:
        by_category.setdefault(classify(t.description), []).append(t)

    lines = []
    for category_name, txs in by_category.items():
        aggregate = aggregate_spend(txs, Frequency.MONTHLY)
        vendor_keys = sorted({merchant_key(t) for t in txs})
        labels = [labels_by_key.get(k, k) for k in vendor_keys]
        vendor_label = ", ".join(labels[:4]) + (f" +{len(labels) - 4} more" if len(labels) > 4 else "")
        lines.append(
            StagedLine(
                description=f"{category_name} (pooled)",
                amount=round(abs(aggregate.average_per_period), 2),
                flow_type=FlowType.EXPENSE,
                frequency=Frequency.MONTHLY,
                account_id=account.id,
                category_name=category_name,
                vendor_keys=vendor_keys,
                vendor_label=vendor_label,
                effective_from=dt.date.today(),
                window_start=aggregate.start_date,
                window_end=aggregate.end_date,
                rationale=(
                    f"Pooled {len(vendor_keys)} small/varied {category_name} vendor(s) on {account.name} "
                    f"({len(txs)} transaction(s) totalling £{-aggregate.total:,.2f}) into one line "
                    "instead of one per vendor."
                ),
            )
        )
    return lines


def _recurring_vendor_lines(expenses: list, account: Account) -> list[StagedLine]:
    tx_dicts = [
        {"date": t.date.isoformat(), "description": t.description, "amount": t.amount} for t in expenses
    ]
    groups = find_recurring_transactions(tx_dicts, min_occurrences=MIN_OCCURRENCES)
    labels_by_key = {v.key: v.label for v in vendor_options(expenses)}

    lines = []
    for group in groups:
        if group["direction"] != "out" or group["avg_amount"] <= 0:
            continue
        spread = (group["max_amount"] - group["min_amount"]) / group["avg_amount"]
        if spread <= TIGHT_SPREAD:
            confidence = "high"
        elif spread <= LOOSE_SPREAD:
            confidence = "medium"
        else:
            continue  # too irregular to call a vendor-level bill — the category catch-all picks this up

        key = group["description"]
        label = labels_by_key.get(key, key)
        category_name = classify(group["sample_descriptions"][0]) or UNCATEGORIZED
        frequency = _FREQUENCY_BY_LABEL.get(group["guessed_frequency"], Frequency.MONTHLY)
        lines.append(
            StagedLine(
                description=label,
                amount=round(group["avg_amount"], 2),
                flow_type=FlowType.EXPENSE,
                frequency=frequency,
                account_id=account.id,
                category_name=category_name,
                vendor_keys=[key],
                vendor_label=label,
                effective_from=dt.date.today(),
                window_start=dt.date.fromisoformat(group["first_date"]),
                window_end=dt.date.fromisoformat(group["last_date"]),
                rationale=(
                    f"{'High' if confidence == 'high' else 'Medium'}-confidence recurring bill on "
                    f"{account.name} — seen {group['occurrences']}x between {group['first_date']} and "
                    f"{group['last_date']}, amounts within {spread:.0%} of £{group['avg_amount']:.2f}."
                ),
            )
        )
    return lines


def _category_catchall_lines(
    session: Session,
    remaining: list,
    account: Account,
    start: dt.date,
    end: dt.date,
    min_monthly_amount: float,
) -> list[StagedLine]:
    categories = {
        t.category_id: t.category for t in remaining if t.category and t.category.name != UNCATEGORIZED
    }

    lines = []
    for category_id, category in categories.items():
        # Anchored to this category's own most recent transaction, not
        # `end` (usually today) — a lag between "last imported statement"
        # and "today" would otherwise land the moving average's last bucket
        # on a stretch with no data at all, silently zeroing the estimate.
        category_end = max(t.date for t in remaining if t.category_id == category_id)
        series = expense_vs_forecast(
            session,
            "Monthly",
            start,
            category_end,
            ma_window=3,
            category_id=category_id,
            account_ids=[account.id],
        )
        if not series.actual_moving_avg:
            continue
        monthly_amount = series.actual_moving_avg[-1]
        if monthly_amount < min_monthly_amount:
            continue
        lines.append(
            StagedLine(
                description=f"{category.name} (recommended)",
                amount=round(monthly_amount, 2),
                flow_type=FlowType.EXPENSE,
                frequency=Frequency.MONTHLY,
                account_id=account.id,
                category_name=category.name,
                vendor_keys=[],
                vendor_label="",
                effective_from=dt.date.today(),
                window_start=start,
                window_end=end,
                rationale=(
                    f"Low-confidence category catch-all — 3-month trailing average of otherwise-"
                    f"uncaptured {category.name} spend on {account.name} is £{monthly_amount:,.2f}/month."
                ),
            )
        )
    return lines


def recommend_budget(
    session: Session,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    account_ids: list[int] | None = None,
    min_monthly_amount: float = MIN_MONTHLY_AMOUNT,
) -> list[StagedLine]:
    """A full slate of StagedLine drafts for the Budget Builder to prime its
    staging area with — see module docstring for the rule tree."""
    end = dt.date.today()
    start = end - dt.timedelta(days=lookback_days)

    accounts_query = session.query(Account)
    if account_ids:
        accounts_query = accounts_query.filter(Account.id.in_(account_ids))
    accounts = accounts_query.order_by(Account.name).all()

    recommendations: list[StagedLine] = []
    for account in accounts:
        account_txs = query_transactions(session, account_ids=[account.id], start_date=start, end_date=end)
        uncaptured = uncaptured_transactions(session, account_txs)
        expenses = [t for t in uncaptured if t.amount < 0]
        if not expenses:
            continue

        lifestyle_txs, individual_txs = _split_lifestyle_transactions(expenses)
        recommendations += _lifestyle_pool_lines(lifestyle_txs, account)

        vendor_lines = _recurring_vendor_lines(individual_txs, account)
        recommendations += vendor_lines

        covered_keys = {line.vendor_keys[0] for line in vendor_lines}
        remaining = [t for t in individual_txs if merchant_key(t) not in covered_keys]
        recommendations += _category_catchall_lines(
            session, remaining, account, start, end, min_monthly_amount
        )

    return recommendations
