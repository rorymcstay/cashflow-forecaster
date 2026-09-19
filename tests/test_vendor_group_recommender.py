import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Account, Base, Statement, Transaction, VendorGroup
from app.vendor_group_recommender import recommend_vendor_groups


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


def _tx(statement, date, description, amount):
    return Transaction(statement=statement, date=date, description=description, amount=amount)


def test_recurring_vendors_in_same_category_are_grouped(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 5), "TESCO STORES", -60.0),
            _tx(statement, dt.date(2026, 2, 5), "TESCO STORES", -55.0),
            _tx(statement, dt.date(2026, 1, 12), "ALDI SUPERMARKET", -40.0),
            _tx(statement, dt.date(2026, 2, 12), "ALDI SUPERMARKET", -42.0),
        ]
    )
    session.commit()

    suggestions = recommend_vendor_groups(session, lookback_days=365)

    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion.name == "Groceries"
    assert set(suggestion.vendor_keys) == {"TESCO STORES", "ALDI SUPERMARKET"}
    assert "Groceries" in suggestion.rationale


def test_single_recurring_vendor_in_a_category_is_not_suggested(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 5), "TESCO STORES", -60.0),
            _tx(statement, dt.date(2026, 2, 5), "TESCO STORES", -55.0),
        ]
    )
    session.commit()

    suggestions = recommend_vendor_groups(session, lookback_days=365)

    assert suggestions == []


def test_vendor_already_in_a_group_is_excluded(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 5), "TESCO STORES", -60.0),
            _tx(statement, dt.date(2026, 2, 5), "TESCO STORES", -55.0),
            _tx(statement, dt.date(2026, 1, 12), "ALDI SUPERMARKET", -40.0),
            _tx(statement, dt.date(2026, 2, 12), "ALDI SUPERMARKET", -42.0),
        ]
    )
    session.add(VendorGroup(name="Supermarkets", vendors="TESCO STORES"))
    session.commit()

    suggestions = recommend_vendor_groups(session, lookback_days=365)

    # Only ALDI remains uncovered — one vendor alone doesn't clear min_vendors.
    assert suggestions == []


def test_non_recurring_and_uncategorized_vendors_are_ignored(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 5), "RANDOM SHOP ONE", -10.0),
            _tx(statement, dt.date(2026, 1, 12), "RANDOM SHOP TWO", -20.0),
        ]
    )
    session.commit()

    suggestions = recommend_vendor_groups(session, lookback_days=365)

    assert suggestions == []
