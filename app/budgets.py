"""Budgets: named, switchable sets of BudgetItem lines.

Exactly one budget is "active" at a time (tracked in the single-row
GlobalOptions table) — every screen, forecast, and report that reads
committed budget items reads only the active budget's, via
`active_budget_items()`. Switching budgets (set_active_budget) therefore
swaps the household's whole recurring-payment plan in one action, without
any of that other code needing to know budgets exist at all.
"""

from sqlalchemy.orm import Query, Session

from app.models import Budget, BudgetItem, BudgetStatus, GlobalOptions

DEFAULT_BUDGET_NAME = "Default"

_GLOBAL_OPTIONS_ID = 1


def ensure_default_budget(session: Session) -> Budget:
    """Guarantee at least one budget and an active selection exist — called
    from init_db() on every startup so a fresh database (or one from before
    multi-budget support existed) always has something valid to point at."""
    budget = session.query(Budget).filter_by(name=DEFAULT_BUDGET_NAME).one_or_none()
    if budget is None:
        budget = Budget(name=DEFAULT_BUDGET_NAME)
        session.add(budget)
        session.flush()

    options = session.get(GlobalOptions, _GLOBAL_OPTIONS_ID)
    if options is None:
        session.add(GlobalOptions(id=_GLOBAL_OPTIONS_ID, active_budget_id=budget.id))
    elif options.active_budget_id is None:
        options.active_budget_id = budget.id
    session.flush()
    return budget


def backfill_unassigned_budget_items(session: Session, fallback: Budget) -> None:
    """Any BudgetItem left over from before the budget_id column existed
    (or created out-of-band) is filed under `fallback` rather than left
    dangling — the column is NOT NULL in the model, so nothing should ever
    stay unassigned past startup."""
    for item in session.query(BudgetItem).filter(BudgetItem.budget_id.is_(None)).all():
        item.budget_id = fallback.id


def get_active_budget(session: Session) -> Budget:
    options = session.get(GlobalOptions, _GLOBAL_OPTIONS_ID)
    budget = session.get(Budget, options.active_budget_id) if options and options.active_budget_id else None
    return budget or ensure_default_budget(session)


def set_active_budget(session: Session, budget_id: int) -> Budget:
    budget = session.get(Budget, budget_id)
    if budget is None:
        raise ValueError(f"No budget with id {budget_id}.")
    options = session.get(GlobalOptions, _GLOBAL_OPTIONS_ID)
    if options is None:
        session.add(GlobalOptions(id=_GLOBAL_OPTIONS_ID, active_budget_id=budget.id))
    else:
        options.active_budget_id = budget.id
    return budget


def list_budgets(session: Session, include_archived: bool = True) -> list[Budget]:
    query = session.query(Budget)
    if not include_archived:
        query = query.filter(Budget.status == BudgetStatus.ACTIVE)
    return query.order_by(Budget.name).all()


def active_budget_items(session: Session) -> Query:
    """Base query for every BudgetItem in the active budget — the one place
    every other module filters from, so switching the active budget never
    requires touching forecast/report/matching code."""
    return session.query(BudgetItem).filter(BudgetItem.budget_id == get_active_budget(session).id)


def create_budget(session: Session, name: str, clone_from: Budget | None = None) -> Budget:
    """A new, empty budget — or, if `clone_from` is given, a copy of every
    one of its budget items ("Save As", for branching an existing plan
    before changing it)."""
    budget = Budget(name=name)
    session.add(budget)
    session.flush()
    if clone_from is not None:
        for item in session.query(BudgetItem).filter_by(budget_id=clone_from.id).all():
            session.add(
                BudgetItem(
                    description=item.description,
                    amount=item.amount,
                    flow_type=item.flow_type,
                    frequency=item.frequency,
                    effective_from=item.effective_from,
                    effective_until=item.effective_until,
                    notes=item.notes,
                    vendors=item.vendors,
                    category_id=item.category_id,
                    account_id=item.account_id,
                    target_account_id=item.target_account_id,
                    budget=budget,
                )
            )
    return budget


def rename_budget(budget: Budget, name: str) -> None:
    budget.name = name


def archive_budget(session: Session, budget: Budget) -> None:
    if get_active_budget(session).id == budget.id:
        raise ValueError("Can't archive the active budget — switch to another budget first.")
    budget.status = BudgetStatus.ARCHIVED


def restore_budget(budget: Budget) -> None:
    budget.status = BudgetStatus.ACTIVE


def delete_budget(session: Session, budget: Budget) -> None:
    if get_active_budget(session).id == budget.id:
        raise ValueError("Can't delete the active budget — switch to another budget first.")
    session.delete(budget)
