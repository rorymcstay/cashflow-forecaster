"""Persisting statement imports: classify + save transactions, update the
account balance, produce a budget-vs-actual report for the billing period,
and maintain budget suggestions generated from transaction history.

This is the persisted counterpart to app/statements.py's ephemeral
extract_csv_transactions / find_recurring_transactions (which do the raw
parsing and pattern-matching only) — this module turns that into saved
Transaction rows, account balance updates, and BudgetItem suggestions.
"""

import calendar
import datetime as dt
from collections import defaultdict

from sqlalchemy.orm import Session

from app.classify import classify
from app.forecast import generate_occurrences
from app.models import (
    Account,
    BudgetItem,
    BudgetSuggestion,
    FlowType,
    Frequency,
    Statement,
    SuggestionStatus,
    SuggestionType,
    Transaction,
    UpcomingExpense,
    UpcomingExpenseStatus,
)
from app.seed import get_or_create_category
from app.statements import find_recurring_transactions, normalize_description

UNCATEGORIZED = "Uncategorized"

# An upcoming expense's amount must match a statement transaction within
# both this fraction and this absolute figure to count as the same event.
AMOUNT_MATCH_TOLERANCE_PCT = 0.02
AMOUNT_MATCH_TOLERANCE_ABS = 0.01

# A recurring group's average amount must differ from the budgeted amount by
# more than both this fraction and this absolute figure to be worth
# suggesting a change — otherwise routine rounding/variation would generate
# noise on every import.
AMOUNT_CHANGE_TOLERANCE_PCT = 0.10
AMOUNT_CHANGE_TOLERANCE_ABS = 2.0

_FREQUENCY_BY_LABEL = {f.value: f for f in Frequency}


def classify_transactions(transactions: list[dict]) -> list[dict]:
    """Attach a best-guess `category` to each transaction dict, by description."""
    return [{**t, "category": classify(str(t.get("description", ""))) or UNCATEGORIZED} for t in transactions]


def match_budget_item(
    session: Session, account_id: int, description: str, as_of: dt.date
) -> BudgetItem | None:
    """Find an active BudgetItem on this account whose description overlaps
    this transaction's — used both to link a transaction to the item it pays
    for, and to check whether a recurring transaction group already has a
    matching budget item.

    Matches on description only, not category: the keyword classifier's
    categories don't always line up with how a budget item happens to be
    filed (e.g. a TV licence transaction classifies as Utilities but is
    commonly budgeted under Subscriptions), so requiring both would produce
    false negatives.
    """
    norm = normalize_description(description)
    if not norm:
        return None
    for item in session.query(BudgetItem).filter_by(account_id=account_id).all():
        if not item.is_active_on(as_of):
            continue
        item_norm = normalize_description(item.description)
        if item_norm and (item_norm in norm or norm in item_norm):
            return item
    return None


def match_upcoming_expense_transaction(statement: Statement, expense: UpcomingExpense) -> Transaction | None:
    """The transaction in `statement` that best matches a scheduled one-off
    expense — same sign, closest amount within tolerance. Amount-based (not
    description-based, unlike match_budget_item) since a one-off's
    description is often generic ("Car repair") and won't textually match
    the real merchant line."""
    expected = expense.amount if expense.flow_type == FlowType.INCOME else -expense.amount
    tolerance = max(abs(expected) * AMOUNT_MATCH_TOLERANCE_PCT, AMOUNT_MATCH_TOLERANCE_ABS)
    best, best_diff = None, None
    for t in statement.transactions:
        if (t.amount > 0) != (expected > 0):
            continue
        diff = abs(t.amount - expected)
        if diff <= tolerance and (best is None or diff < best_diff):
            best, best_diff = t, diff
    return best


def reconcile_upcoming_expenses(session: Session, account: Account, statement: Statement) -> None:
    """For every still-Pending one-off expense scheduled within this
    statement's period: auto-archive it if a matching transaction actually
    came in, otherwise flag it Needs Review — the schedule said it should
    have happened by now, and nothing in the real data matches, so a human
    needs to decide whether to reschedule it or archive it."""
    pending = (
        session.query(UpcomingExpense)
        .filter(UpcomingExpense.account_id == account.id)
        .filter(UpcomingExpense.status == UpcomingExpenseStatus.PENDING)
        .filter(UpcomingExpense.date >= statement.period_start)
        .filter(UpcomingExpense.date <= statement.period_end)
        .all()
    )
    for expense in pending:
        match = match_upcoming_expense_transaction(statement, expense)
        expense.last_seen_statement = statement
        if match is not None:
            expense.status = UpcomingExpenseStatus.ARCHIVED
            expense.matched_transaction = match
        else:
            expense.status = UpcomingExpenseStatus.NEEDS_REVIEW


def import_statement(
    session: Session,
    account: Account,
    transactions: list[dict],
    period_start: dt.date,
    period_end: dt.date,
    source_note: str | None = None,
) -> Statement:
    """Classify and persist a billing period's transactions, update the
    account balance, and refresh this account's budget suggestions.

    Statements must be imported in chronological order per account: the
    account's `current_balance` is incremented by the net of the imported
    transactions and `balance_as_of` is advanced to `period_end`, so a
    statement whose period starts before the account's current
    `balance_as_of` would double-count history that's already reflected in
    the balance.
    """
    existing = (
        session.query(Statement)
        .filter_by(account_id=account.id, period_start=period_start, period_end=period_end)
        .one_or_none()
    )
    if existing is not None:
        raise ValueError(
            f"A statement for '{account.name}' covering {period_start.isoformat()} to "
            f"{period_end.isoformat()} has already been imported (statement #{existing.id})."
        )
    if period_start < account.balance_as_of:
        raise ValueError(
            f"'{account.name}' balance is already known as of {account.balance_as_of.isoformat()}, "
            f"which is after this statement's start ({period_start.isoformat()}). Statements must be "
            "imported in chronological order per account, oldest first, so the balance update doesn't "
            "double-count. Import earlier statements first, or use update_account to correct the "
            "balance-as-of date if this is a deliberate backfill."
        )

    classified = classify_transactions(transactions)

    statement = Statement(
        account=account, period_start=period_start, period_end=period_end, source_note=source_note
    )
    session.add(statement)
    session.flush()

    net = 0.0
    for t in classified:
        raw_date = t["date"]
        date = raw_date if isinstance(raw_date, dt.date) else dt.date.fromisoformat(raw_date)
        amount = float(t["amount"])
        net += amount
        category = get_or_create_category(session, t["category"])
        matched = match_budget_item(session, account.id, str(t["description"]), date)
        session.add(
            Transaction(
                statement=statement,
                date=date,
                description=str(t["description"]),
                amount=amount,
                category=category,
                matched_budget_item=matched,
            )
        )

    account.current_balance += net
    account.balance_as_of = period_end
    reconcile_upcoming_expenses(session, account, statement)
    session.commit()

    generate_suggestions(session, account)
    session.commit()
    session.refresh(statement)
    return statement


def _transaction_summary(t: Transaction) -> dict:
    return {
        "id": t.id,
        "date": t.date.isoformat(),
        "description": t.description,
        "amount": t.amount,
        "category": t.category.name if t.category else None,
    }


def budget_vs_actual_report(session: Session, statement: Statement) -> dict:
    """Budgeted vs actual spend/income for a statement's account + period.

    `variance = actual - budgeted`: for an expense category (both figures
    negative) a negative variance means overspending; for income a negative
    variance means underearning relative to budget.
    """
    account = statement.account
    items = session.query(BudgetItem).filter_by(account_id=account.id).all()
    items_by_id = {i.id: i for i in items}

    expected_by_item: dict[int, float] = {}
    for item in items:
        occurrences = generate_occurrences(
            item.effective_from,
            item.effective_until,
            item.frequency,
            statement.period_start,
            statement.period_end,
        )
        if not occurrences:
            continue
        signed = item.amount if item.flow_type == FlowType.INCOME else -item.amount
        expected_by_item[item.id] = signed * len(occurrences)

    actual_by_item: dict[int, float] = defaultdict(float)
    unmatched: list[Transaction] = []
    uncategorized: list[Transaction] = []
    for t in statement.transactions:
        if t.matched_budget_item_id is not None:
            actual_by_item[t.matched_budget_item_id] += t.amount
        else:
            unmatched.append(t)
        if t.category is None or t.category.name == UNCATEGORIZED:
            uncategorized.append(t)

    category_rows: dict[str, dict] = defaultdict(lambda: {"budgeted": 0.0, "actual": 0.0})
    for item_id in set(expected_by_item) | set(actual_by_item):
        item = items_by_id.get(item_id)
        cat_name = item.category.name if item else "Unknown"
        category_rows[cat_name]["budgeted"] += expected_by_item.get(item_id, 0.0)
        category_rows[cat_name]["actual"] += actual_by_item.get(item_id, 0.0)

    if unmatched:
        category_rows["Unbudgeted"]["actual"] += sum(t.amount for t in unmatched)

    by_category = [
        {
            "category": cat,
            "budgeted": round(v["budgeted"], 2),
            "actual": round(v["actual"], 2),
            "variance": round(v["actual"] - v["budgeted"], 2),
        }
        for cat, v in sorted(category_rows.items())
    ]
    total_budgeted = sum(v["budgeted"] for v in category_rows.values())
    total_actual = sum(v["actual"] for v in category_rows.values())

    return {
        "statement_id": statement.id,
        "account": account.name,
        "period_start": statement.period_start.isoformat(),
        "period_end": statement.period_end.isoformat(),
        "by_category": by_category,
        "total_budgeted": round(total_budgeted, 2),
        "total_actual": round(total_actual, 2),
        "total_variance": round(total_actual - total_budgeted, 2),
        "unmatched_transactions": [_transaction_summary(t) for t in unmatched],
        "uncategorized_transactions": [_transaction_summary(t) for t in uncategorized],
    }


def _guessed_to_frequency(label: str) -> Frequency:
    return _FREQUENCY_BY_LABEL.get(label, Frequency.MONTHLY)


def _infer_category(group: dict) -> str:
    sample = group["sample_descriptions"][0] if group["sample_descriptions"] else group["description"]
    return classify(sample) or UNCATEGORIZED


def _upsert_suggestion(
    session: Session,
    account: Account,
    suggestion_type: SuggestionType,
    description: str,
    category_name: str,
    proposed_amount: float,
    proposed_frequency: Frequency,
    budget_item: BudgetItem | None,
    current_amount: float | None,
    rationale: str,
) -> BudgetSuggestion | None:
    existing = (
        session.query(BudgetSuggestion)
        .filter_by(account_id=account.id, description=description, suggestion_type=suggestion_type)
        .order_by(BudgetSuggestion.created_at.desc())
        .first()
    )
    if existing is not None:
        if existing.status == SuggestionStatus.PENDING:
            existing.proposed_amount = round(proposed_amount, 2)
            existing.proposed_frequency = proposed_frequency
            existing.rationale = rationale
            existing.category = get_or_create_category(session, category_name)
            return existing
        if existing.status == SuggestionStatus.REJECTED:
            return None  # respect the user's decision — don't re-litigate

    suggestion = BudgetSuggestion(
        suggestion_type=suggestion_type,
        status=SuggestionStatus.PENDING,
        account=account,
        category=get_or_create_category(session, category_name),
        description=description,
        proposed_amount=round(proposed_amount, 2),
        proposed_frequency=proposed_frequency,
        budget_item=budget_item,
        current_amount=current_amount,
        rationale=rationale,
    )
    session.add(suggestion)
    return suggestion


def generate_suggestions(session: Session, account: Account) -> list[BudgetSuggestion]:
    """Re-derive budget suggestions for this account from its full imported
    transaction history (not just the statement just imported — more history
    gives a stronger recurring-transaction signal over time).
    """
    rows = session.query(Transaction).join(Statement).filter(Statement.account_id == account.id).all()
    tx_dicts = [{"date": t.date.isoformat(), "description": t.description, "amount": t.amount} for t in rows]
    groups = find_recurring_transactions(tx_dicts)

    touched: list[BudgetSuggestion] = []
    for group in groups:
        description = group["description"]
        last_date = dt.date.fromisoformat(group["last_date"])
        matched_item = match_budget_item(session, account.id, description, last_date)

        if matched_item is None:
            if group["direction"] == "in":
                continue  # only auto-suggest expense items — income recurrences are usually salary/transfers
            suggestion = _upsert_suggestion(
                session,
                account,
                SuggestionType.NEW_ITEM,
                description=description,
                category_name=_infer_category(group),
                proposed_amount=group["avg_amount"],
                proposed_frequency=_guessed_to_frequency(group["guessed_frequency"]),
                budget_item=None,
                current_amount=None,
                rationale=(
                    f"Seen {group['occurrences']}x between {group['first_date']} and "
                    f"{group['last_date']}, avg £{group['avg_amount']:.2f}, no matching budget item."
                ),
            )
        else:
            diff = abs(matched_item.amount - group["avg_amount"])
            if (
                diff <= AMOUNT_CHANGE_TOLERANCE_ABS
                or diff <= matched_item.amount * AMOUNT_CHANGE_TOLERANCE_PCT
            ):
                continue
            suggestion = _upsert_suggestion(
                session,
                account,
                SuggestionType.AMOUNT_CHANGE,
                description=matched_item.description,
                category_name=matched_item.category.name,
                proposed_amount=group["avg_amount"],
                proposed_frequency=matched_item.frequency,
                budget_item=matched_item,
                current_amount=matched_item.amount,
                rationale=(
                    f"Actual avg £{group['avg_amount']:.2f} over {group['occurrences']} occurrences "
                    f"differs from budgeted £{matched_item.amount:.2f}."
                ),
            )
        if suggestion is not None:
            touched.append(suggestion)

    session.commit()
    return touched


def accept_suggestion(session: Session, suggestion: BudgetSuggestion) -> BudgetItem:
    """Apply a pending suggestion: create a new BudgetItem (NEW_ITEM) or
    update the linked one's amount (AMOUNT_CHANGE). effective_from for a new
    item is today, so accepting doesn't retroactively change past budget
    summaries. Raises ValueError if the suggestion isn't pending."""
    if suggestion.status != SuggestionStatus.PENDING:
        raise ValueError(f"Suggestion #{suggestion.id} is already {suggestion.status.value}.")

    if suggestion.suggestion_type == SuggestionType.NEW_ITEM:
        item = BudgetItem(
            description=suggestion.description,
            amount=suggestion.proposed_amount,
            flow_type=FlowType.EXPENSE,
            frequency=suggestion.proposed_frequency,
            effective_from=dt.date.today(),
            category=suggestion.category,
            account=suggestion.account,
        )
        session.add(item)
        suggestion.budget_item = item
    else:
        item = suggestion.budget_item
        if item is None:
            raise ValueError(f"Suggestion #{suggestion.id} has no linked budget item.")
        item.amount = suggestion.proposed_amount

    suggestion.status = SuggestionStatus.ACCEPTED
    suggestion.decided_at = dt.datetime.now(dt.UTC)
    session.commit()
    return item


def reject_suggestion(session: Session, suggestion: BudgetSuggestion) -> None:
    """Mark a pending suggestion rejected. Raises ValueError if it isn't pending."""
    if suggestion.status != SuggestionStatus.PENDING:
        raise ValueError(f"Suggestion #{suggestion.id} is already {suggestion.status.value}.")
    suggestion.status = SuggestionStatus.REJECTED
    suggestion.decided_at = dt.datetime.now(dt.UTC)
    session.commit()


def detect_account_for_hint(accounts: list[Account], hint: str | None) -> int | None:
    """Match a parsed statement's bank-name hint (e.g. "HSBC", "Amex") against
    existing accounts by a case-insensitive substring of the account name.
    Returns the id only when exactly one account matches — an ambiguous or
    empty match is left for the user to pick explicitly, since silently
    guessing wrong would misfile a statement's transactions."""
    if not hint:
        return None
    matches = [a.id for a in accounts if hint.lower() in a.name.lower()]
    return matches[0] if len(matches) == 1 else None


def _month_start(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def _next_month(d: dt.date) -> dt.date:
    return dt.date(d.year + 1, 1, 1) if d.month == 12 else dt.date(d.year, d.month + 1, 1)


def find_statement_gaps(
    session: Session, account_id: int, start_date: dt.date, end_date: dt.date
) -> list[dict]:
    """Calendar months between start_date and end_date that no imported
    Statement for this account overlaps at all — billing cycles rarely line
    up with calendar months, so "gap" here means the whole month has no
    statement coverage, not that one specific day is missing."""
    statements = (
        session.query(Statement)
        .filter(Statement.account_id == account_id)
        .filter(Statement.period_end >= start_date)
        .filter(Statement.period_start <= end_date)
        .all()
    )
    gaps = []
    month = _month_start(start_date)
    while month <= end_date:
        days_in_month = calendar.monthrange(month.year, month.month)[1]
        month_end = dt.date(month.year, month.month, days_in_month)
        covered = any(s.period_start <= month_end and s.period_end >= month for s in statements)
        if not covered:
            gaps.append({"month": month.strftime("%Y-%m"), "label": month.strftime("%B %Y")})
        month = _next_month(month)
    return gaps
