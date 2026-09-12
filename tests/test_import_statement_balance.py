import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Account, Base, FlowType, Statement, UpcomingExpense, UpcomingExpenseStatus
from app.statement_import import import_statement


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _tx(date: str, description: str, amount: float) -> dict:
    return {"date": date, "description": description, "amount": amount}


def test_forward_import_updates_balance_and_advances_balance_as_of(session):
    account = Account(name="Personal HSBC", current_balance=100.0, balance_as_of=dt.date(2026, 1, 1))
    session.add(account)
    session.commit()

    import_statement(
        session,
        account,
        [_tx("2026-01-10", "Salary", 500.0), _tx("2026-01-15", "Rent", -300.0)],
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 31),
    )

    assert account.current_balance == 300.0
    assert account.balance_as_of == dt.date(2026, 1, 31)


def test_historical_import_leaves_balance_untouched(session):
    account = Account(name="Personal HSBC", current_balance=300.0, balance_as_of=dt.date(2026, 1, 31))
    session.add(account)
    session.commit()

    statement = import_statement(
        session,
        account,
        [_tx("2025-12-10", "Salary", 500.0), _tx("2025-12-15", "Rent", -300.0)],
        dt.date(2025, 12, 1),
        dt.date(2025, 12, 31),
    )

    # balance/balance_as_of unchanged...
    assert account.current_balance == 300.0
    assert account.balance_as_of == dt.date(2026, 1, 31)
    # ...but the transactions were still saved for history/reporting.
    assert len(statement.transactions) == 2
    saved = session.query(Statement).filter_by(id=statement.id).one()
    assert len(saved.transactions) == 2


def test_historical_import_boundary_period_end_equals_balance_as_of(session):
    """period_end == balance_as_of is still "already known" — inclusive boundary."""
    account = Account(name="Personal HSBC", current_balance=300.0, balance_as_of=dt.date(2026, 1, 31))
    session.add(account)
    session.commit()

    import_statement(
        session,
        account,
        [_tx("2026-01-31", "Late fee", -10.0)],
        dt.date(2026, 1, 20),
        dt.date(2026, 1, 31),
    )

    assert account.current_balance == 300.0
    assert account.balance_as_of == dt.date(2026, 1, 31)


def test_gap_before_period_still_advances_balance(session):
    """A gap between balance_as_of and period_start is allowed — the new
    statement's net still applies and balance_as_of jumps to its period_end,
    since the Missing Statements panel is what surfaces the gap itself."""
    account = Account(name="Personal HSBC", current_balance=300.0, balance_as_of=dt.date(2026, 1, 31))
    session.add(account)
    session.commit()

    import_statement(
        session,
        account,
        [_tx("2026-03-10", "Salary", 500.0)],
        dt.date(2026, 3, 1),
        dt.date(2026, 3, 31),
    )

    assert account.current_balance == 800.0
    assert account.balance_as_of == dt.date(2026, 3, 31)


def test_straddling_period_trims_to_the_unknown_tail_instead_of_rejecting(session):
    """A rolling CSV re-export (e.g. Monzo "last 3 months") will always
    straddle whatever's already imported — rather than reject it, drop the
    already-known portion and apply only the new tail."""
    account = Account(name="Joint Monzo", current_balance=300.0, balance_as_of=dt.date(2026, 1, 15))
    session.add(account)
    session.commit()

    statement = import_statement(
        session,
        account,
        [
            _tx("2026-01-05", "Already known", 999.0),  # before balance_as_of — dropped
            _tx("2026-01-15", "Also already known", 999.0),  # on balance_as_of — dropped (inclusive)
            _tx("2026-01-20", "Salary", 500.0),
            _tx("2026-01-25", "Rent", -300.0),
        ],
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 31),
    )

    assert [t.description for t in statement.transactions] == ["Salary", "Rent"]
    assert statement.period_start == dt.date(2026, 1, 16)  # balance_as_of + 1 day
    assert statement.period_end == dt.date(2026, 1, 31)
    assert account.current_balance == 500.0  # 300 + (500 - 300), the 999s never counted
    assert account.balance_as_of == dt.date(2026, 1, 31)


def test_straddling_period_with_nothing_new_raises(session):
    account = Account(name="Joint Monzo", current_balance=300.0, balance_as_of=dt.date(2026, 1, 15))
    session.add(account)
    session.commit()

    with pytest.raises(ValueError, match="nothing new to import"):
        import_statement(
            session,
            account,
            [_tx("2026-01-05", "Already known", 999.0)],
            dt.date(2026, 1, 1),
            dt.date(2026, 1, 31),
        )

    assert account.current_balance == 300.0
    assert account.balance_as_of == dt.date(2026, 1, 15)
    assert session.query(Statement).count() == 0


def test_reimporting_the_front_statement_upserts_transactions_and_reapplies_net(session):
    """Re-importing the statement that currently defines balance_as_of is the
    supported way to correct it — replace-and-reapply, not reject."""
    account = Account(name="Personal HSBC", current_balance=0.0, balance_as_of=dt.date(2026, 1, 1))
    session.add(account)
    session.commit()
    import_statement(
        session, account, [_tx("2026-01-10", "x", 100.0)], dt.date(2026, 1, 1), dt.date(2026, 1, 31)
    )
    assert account.current_balance == 100.0

    statement = import_statement(
        session, account, [_tx("2026-01-10", "x corrected", 250.0)], dt.date(2026, 1, 1), dt.date(2026, 1, 31)
    )

    # old net (100) reversed, new net (250) applied — not stacked on top.
    assert account.current_balance == 250.0
    assert account.balance_as_of == dt.date(2026, 1, 31)
    assert [t.description for t in statement.transactions] == ["x corrected"]
    assert session.query(Statement).count() == 1


def test_reimporting_front_statement_with_closing_balance_self_heals_prior_drift(session):
    """Simulates the real bug: the account was primed with a sign-flipped
    balance before the last import. Re-importing that same statement with a
    known closing_balance (e.g. re-read off the PDF) fixes it directly,
    regardless of how wrong current_balance had drifted."""
    account = Account(name="Personal Amex", current_balance=1356.71, balance_as_of=dt.date(2026, 8, 5))
    session.add(account)
    session.commit()
    import_statement(
        session, account, [_tx("2026-08-20", "spend", -3252.71)], dt.date(2026, 8, 6), dt.date(2026, 9, 5)
    )
    assert account.current_balance == round(1356.71 - 3252.71, 2)  # wrong, inherited the bad anchor

    import_statement(
        session,
        account,
        [_tx("2026-08-20", "spend", -3252.71)],
        dt.date(2026, 8, 6),
        dt.date(2026, 9, 5),
        closing_balance=-4609.42,
    )

    assert account.current_balance == -4609.42
    assert account.balance_as_of == dt.date(2026, 9, 5)


def test_reimporting_historical_statement_upserts_without_touching_balance(session):
    account = Account(name="Personal HSBC", current_balance=300.0, balance_as_of=dt.date(2026, 2, 28))
    session.add(account)
    session.commit()
    import_statement(
        session, account, [_tx("2026-01-10", "old", 10.0)], dt.date(2026, 1, 1), dt.date(2026, 1, 31)
    )

    statement = import_statement(
        session, account, [_tx("2026-01-10", "corrected", 25.0)], dt.date(2026, 1, 1), dt.date(2026, 1, 31)
    )

    assert account.current_balance == 300.0  # untouched — this period is historical
    assert account.balance_as_of == dt.date(2026, 2, 28)
    assert [t.description for t in statement.transactions] == ["corrected"]


def test_reimport_that_would_straddle_is_trimmed_too(session):
    account = Account(name="Joint Monzo", current_balance=0.0, balance_as_of=dt.date(2026, 1, 1))
    session.add(account)
    session.commit()
    import_statement(
        session, account, [_tx("2026-01-10", "x", 1.0)], dt.date(2026, 1, 1), dt.date(2026, 1, 31)
    )
    # Someone rewinds the known date to mid-January by other means.
    account.balance_as_of = dt.date(2026, 1, 15)
    session.commit()

    statement = import_statement(
        session,
        account,
        [_tx("2026-01-10", "x", 1.0), _tx("2026-01-20", "y", 50.0)],
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 31),
    )

    assert [t.description for t in statement.transactions] == ["y"]
    assert statement.period_start == dt.date(2026, 1, 16)
    assert account.current_balance == 51.0  # 1.0 (first import's net) + 50.0 (trimmed second import's net)
    assert account.balance_as_of == dt.date(2026, 1, 31)


def test_out_of_order_backfill_then_forward_reconciles(session):
    """Import Feb (forward), then Jan (historical backfill) — final balance
    should equal what a strictly-chronological Jan-then-Feb import would give."""
    account = Account(name="Personal HSBC", current_balance=100.0, balance_as_of=dt.date(2026, 1, 1))
    session.add(account)
    session.commit()

    import_statement(
        session, account, [_tx("2026-02-10", "Feb income", 200.0)], dt.date(2026, 2, 1), dt.date(2026, 2, 28)
    )
    assert account.current_balance == 300.0
    assert account.balance_as_of == dt.date(2026, 2, 28)

    # Backfilling January shouldn't touch the balance we already advanced to Feb.
    import_statement(
        session, account, [_tx("2026-01-15", "Jan income", 50.0)], dt.date(2026, 1, 1), dt.date(2026, 1, 31)
    )
    assert account.current_balance == 300.0
    assert account.balance_as_of == dt.date(2026, 2, 28)

    total_transactions = sum(len(s.transactions) for s in session.query(Statement).all())
    assert total_transactions == 2


def test_reimport_unmatches_upcoming_expense_pointing_at_a_replaced_transaction(session):
    from app.models import Category

    account = Account(name="Personal HSBC", current_balance=0.0, balance_as_of=dt.date(2026, 1, 1))
    category = Category(name="Bills")
    session.add_all([account, category])
    session.flush()
    expense = UpcomingExpense(
        date=dt.date(2026, 1, 10),
        description="Council tax",
        amount=100.0,
        flow_type=FlowType.EXPENSE,
        status=UpcomingExpenseStatus.PENDING,
        category=category,
        account=account,
    )
    session.add(expense)
    session.commit()

    import_statement(
        session,
        account,
        [_tx("2026-01-10", "Council tax", -100.0)],
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 31),
    )
    session.refresh(expense)
    assert expense.status == UpcomingExpenseStatus.ARCHIVED
    old_matched_id = expense.matched_transaction_id
    assert old_matched_id is not None

    # Re-import with a differently-described (but still matching) transaction.
    import_statement(
        session,
        account,
        [_tx("2026-01-10", "Council Tax DD", -100.0)],
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 31),
    )
    session.refresh(expense)

    assert expense.status == UpcomingExpenseStatus.ARCHIVED
    assert expense.matched_transaction_id is not None
    # re-matched against the fresh transaction, not left pointing at stale data
    assert expense.matched_transaction.description == "Council Tax DD"
    # exactly one transaction survives for this statement — the old one was
    # actually replaced, not left behind as an orphan alongside the new one
    # (note: SQLite may reuse the freed rowid, so ids alone can't prove this)
    statement = session.query(Statement).one()
    assert len(statement.transactions) == 1
