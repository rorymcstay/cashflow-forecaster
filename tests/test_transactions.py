import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Account, Base, Category, Statement, Transaction
from app.transactions import (
    UNCATEGORIZED_ID,
    merchant_key,
    query_transactions,
    recurring_groups_by_merchant,
    similar_transactions,
    spend_breakdown,
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


def test_query_transactions_filters_by_category_including_uncategorized(session):
    account = Account(name="Current")
    groceries = Category(name="Groceries")
    transfers = Category(name="Transfers")
    session.add_all([account, groceries, transfers])
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 5), "TESCO STORES", -20.0, category=groceries),
            _tx(statement, dt.date(2026, 1, 6), "SAVINGS SWEEP", -100.0, category=transfers),
            _tx(statement, dt.date(2026, 1, 7), "UNKNOWN MERCHANT", -5.0, category=None),
        ]
    )
    session.commit()

    # Real category only.
    only_groceries = query_transactions(session, category_ids=[groceries.id])
    assert {t.description for t in only_groceries} == {"TESCO STORES"}

    # Everything except Transfers (Groceries + the uncategorized row).
    excluding_transfers = query_transactions(session, category_ids=[groceries.id, UNCATEGORIZED_ID])
    assert {t.description for t in excluding_transfers} == {"TESCO STORES", "UNKNOWN MERCHANT"}

    # Uncategorized-only.
    only_uncategorized = query_transactions(session, category_ids=[UNCATEGORIZED_ID])
    assert {t.description for t in only_uncategorized} == {"UNKNOWN MERCHANT"}


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


def test_spend_breakdown_totals_and_category_split(session):
    account = Account(name="Current")
    groceries = Category(name="Groceries")
    dining = Category(name="Dining")
    session.add_all([account, groceries, dining])
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 5), "Salary", 2000.0),
            _tx(statement, dt.date(2026, 1, 6), "TESCO STORES", -60.0, category=groceries),
            _tx(statement, dt.date(2026, 1, 7), "TESCO STORES", -40.0, category=groceries),
            _tx(statement, dt.date(2026, 1, 8), "CAFE NERO", -20.0, category=dining),
        ]
    )
    session.commit()

    result = spend_breakdown(query_transactions(session))

    assert result["total_income"] == 2000.0
    assert result["total_expense"] == 120.0
    assert result["net"] == 1880.0
    assert result["transaction_count"] == 4
    assert result["by_category"] == [
        {"category": "Groceries", "amount": 100.0, "pct": pytest.approx(83.3, abs=0.1)},
        {"category": "Dining", "amount": 20.0, "pct": pytest.approx(16.7, abs=0.1)},
    ]
    assert result["months_spanned"] == 1
    assert result["avg_monthly_expense"] == 120.0


def test_spend_breakdown_merchant_and_monthly_trend(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 2, 28))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 10), "NETFLIX.COM 123456", -9.99),
            _tx(statement, dt.date(2026, 2, 10), "NETFLIX.COM 654321", -9.99),
            _tx(statement, dt.date(2026, 1, 15), "AMAZON", -30.0),
            _tx(statement, dt.date(2026, 2, 1), "Salary", 1000.0),
        ]
    )
    session.commit()

    result = spend_breakdown(query_transactions(session))

    # AMAZON (30.0, one-off) outspends NETFLIX (19.98 total across 2 months)
    assert result["top_merchants"][0]["merchant"] == "AMAZON"
    netflix_row = next(r for r in result["top_merchants"] if r["merchant"].startswith("NETFLIX"))
    assert netflix_row["count"] == 2
    assert netflix_row["amount"] == pytest.approx(19.98)
    assert result["months_spanned"] == 2
    by_month = {row["month"]: row for row in result["by_month"]}
    assert by_month["2026-01"]["expense"] == pytest.approx(39.99)
    assert by_month["2026-02"]["income"] == 1000.0
    assert by_month["2026-02"]["expense"] == pytest.approx(9.99)


def test_spend_breakdown_empty_is_zeroed_not_crashing(session):
    result = spend_breakdown([])
    assert result["total_income"] == 0.0
    assert result["total_expense"] == 0.0
    assert result["by_category"] == []
    assert result["by_month"] == []
    assert result["months_spanned"] == 1  # avoids a divide-by-zero on the averages
    assert result["avg_monthly_expense"] == 0.0
