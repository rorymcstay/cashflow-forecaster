import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Account, Base, Statement, Transaction
from app.transactions import (
    merchant_key,
    query_transactions,
    recurring_groups_by_merchant,
    similar_transactions,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _make_statement(session, account, start, end):
    statement = Statement(account=account, period_start=start, period_end=end)
    session.add(statement)
    session.flush()
    return statement


def _tx(statement, date, description, amount, category=None):
    return Transaction(
        statement=statement, date=date, description=description, amount=amount, category=category
    )


def test_query_transactions_filters_by_account_and_date(session):
    a1 = Account(name="Current")
    a2 = Account(name="Savings")
    session.add_all([a1, a2])
    session.flush()

    s1 = _make_statement(session, a1, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    s2 = _make_statement(session, a2, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    session.add_all(
        [
            _tx(s1, dt.date(2026, 1, 5), "TESCO STORES", -20.0),
            _tx(s1, dt.date(2026, 2, 5), "TESCO STORES", -25.0),
            _tx(s2, dt.date(2026, 1, 5), "SAVINGS INTEREST", 1.0),
        ]
    )
    session.commit()

    all_tx = query_transactions(session)
    assert len(all_tx) == 3

    only_a1 = query_transactions(session, account_ids=[a1.id])
    assert {t.description for t in only_a1} == {"TESCO STORES"}

    only_january = query_transactions(session, start_date=dt.date(2026, 1, 1), end_date=dt.date(2026, 1, 31))
    assert len(only_january) == 2


def test_similar_transactions_groups_by_normalised_merchant(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))

    netflix_jan = _tx(statement, dt.date(2026, 1, 10), "NETFLIX.COM 123456", -9.99)
    netflix_feb = _tx(statement, dt.date(2026, 2, 10), "NETFLIX.COM 654321", -9.99)
    tesco = _tx(statement, dt.date(2026, 1, 15), "TESCO STORES", -20.0)
    session.add_all([netflix_jan, netflix_feb, tesco])
    session.commit()

    pool = query_transactions(session)
    matches = similar_transactions(netflix_jan, pool)

    assert matches == [netflix_feb]
    assert merchant_key(netflix_jan) == merchant_key(netflix_feb)
    assert tesco not in matches


def test_similar_transactions_excludes_itself_and_handles_blank_description(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    blank = _tx(statement, dt.date(2026, 1, 1), "", -1.0)
    session.add(blank)
    session.commit()

    pool = query_transactions(session)
    assert similar_transactions(blank, pool) == []


def test_recurring_groups_by_merchant_flags_repeated_bills(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 4, 30))
    rows = [
        _tx(statement, dt.date(2026, 1, 1), "NETFLIX.COM", -9.99),
        _tx(statement, dt.date(2026, 2, 1), "NETFLIX.COM", -9.99),
        _tx(statement, dt.date(2026, 3, 1), "NETFLIX.COM", -9.99),
        _tx(statement, dt.date(2026, 1, 20), "ONE OFF PURCHASE", -50.0),
    ]
    session.add_all(rows)
    session.commit()

    groups = recurring_groups_by_merchant(query_transactions(session))

    assert "NETFLIX COM" in groups
    assert groups["NETFLIX COM"]["occurrences"] == 3
    assert groups["NETFLIX COM"]["guessed_frequency"] == "Monthly"
    assert "ONE OFF PURCHASE" not in groups
