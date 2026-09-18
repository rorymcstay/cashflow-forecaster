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
      -> whatever's left, grouped by category: take the 3-month trailing
         moving average of that category's monthly spend on this account
         (app.analytics.expense_vs_forecast) and, if it clears
         MIN_MONTHLY_AMOUNT, propose a whole-category monthly line
         ("category_catchall", low confidence)
"""

import datetime as dt

from sqlalchemy.orm import Session

from app.analytics import expense_vs_forecast
from app.budget_builder import StagedLine, uncaptured_transactions, vendor_options
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

_FREQUENCY_BY_LABEL = {f.value: f for f in Frequency}


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

        vendor_lines = _recurring_vendor_lines(expenses, account)
        recommendations += vendor_lines

        covered_keys = {line.vendor_keys[0] for line in vendor_lines}
        remaining = [t for t in expenses if merchant_key(t) not in covered_keys]
        recommendations += _category_catchall_lines(
            session, remaining, account, start, end, min_monthly_amount
        )

    return recommendations
