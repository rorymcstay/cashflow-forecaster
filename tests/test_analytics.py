import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analytics import expense_vs_forecast
from app.models import (
    Account,
    Base,
    BudgetItem,
    Category,
    FlowType,
    Frequency,
    Statement,
    Transaction,
    VendorGroup,
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


def test_monthly_buckets_are_gap_free_and_sum_actual_expenses(session):
    account = Account(name="Current")
    groceries = Category(name="Groceries")
    session.add_all([account, groceries])
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 5), "TESCO STORES", -20.0, category=groceries),
            _tx(statement, dt.date(2026, 1, 20), "TESCO STORES", -10.0, category=groceries),
            _tx(statement, dt.date(2026, 3, 5), "TESCO STORES", -15.0, category=groceries),
            _tx(statement, dt.date(2026, 1, 6), "SALARY", 2000.0, category=groceries),  # income excluded
        ]
    )
    session.commit()

    series = expense_vs_forecast(
        session, "Monthly", dt.date(2026, 1, 1), dt.date(2026, 3, 31), category_id=groceries.id
    )

    assert series.bucket_labels == ["Jan 2026", "Feb 2026", "Mar 2026"]
    assert series.actual == [30.0, 0.0, 15.0]
    assert series.matched_transaction_count == 3


def test_forecast_uses_matching_category_budget_item_occurrences(session):
    account = Account(name="Current")
    groceries = Category(name="Groceries")
    session.add_all([account, groceries])
    session.flush()
    session.add(
        BudgetItem(
            description="Groceries budget",
            amount=100.0,
            flow_type=FlowType.EXPENSE,
            frequency=Frequency.MONTHLY,
            effective_from=dt.date(2026, 1, 1),
            category=groceries,
            account=account,
        )
    )
    session.commit()

    series = expense_vs_forecast(
        session, "Monthly", dt.date(2026, 1, 1), dt.date(2026, 3, 31), category_id=groceries.id
    )

    assert series.forecast == [100.0, 100.0, 100.0]
    assert series.matched_budget_item_ids


def test_vendor_group_filter_only_matches_vendor_scoped_items_and_transactions(session):
    account = Account(name="Current")
    groceries = Category(name="Groceries")
    session.add_all([account, groceries])
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 5), "TESCO STORES", -20.0, category=groceries),
            _tx(statement, dt.date(2026, 1, 6), "ALDI STORES", -10.0, category=groceries),
        ]
    )
    group = VendorGroup(name="Big Supermarkets", vendors="TESCO STORES")
    session.add(group)
    session.flush()

    scoped_item = BudgetItem(
        description="Tesco",
        amount=50.0,
        flow_type=FlowType.EXPENSE,
        frequency=Frequency.MONTHLY,
        effective_from=dt.date(2026, 1, 1),
        category=groceries,
        account=account,
    )
    scoped_item.vendor_list = ["TESCO STORES"]
    unscoped_item = BudgetItem(
        description="Whole category",
        amount=999.0,
        flow_type=FlowType.EXPENSE,
        frequency=Frequency.MONTHLY,
        effective_from=dt.date(2026, 1, 1),
        category=groceries,
        account=account,
    )
    session.add_all([scoped_item, unscoped_item])
    session.commit()

    series = expense_vs_forecast(
        session, "Monthly", dt.date(2026, 1, 1), dt.date(2026, 1, 31), vendor_group_id=group.id
    )

    assert series.actual == [20.0]  # only the TESCO transaction, not ALDI
    assert series.forecast == [50.0]  # only the vendor-scoped item, not the unscoped one
    assert series.matched_budget_item_ids == [scoped_item.id]


def test_moving_average_grows_window_from_the_start(session):
    account = Account(name="Current")
    groceries = Category(name="Groceries")
    session.add_all([account, groceries])
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 4, 30))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 15), "SHOP", -10.0, category=groceries),
            _tx(statement, dt.date(2026, 2, 15), "SHOP", -20.0, category=groceries),
            _tx(statement, dt.date(2026, 3, 15), "SHOP", -30.0, category=groceries),
            _tx(statement, dt.date(2026, 4, 15), "SHOP", -40.0, category=groceries),
        ]
    )
    session.commit()

    series = expense_vs_forecast(
        session,
        "Monthly",
        dt.date(2026, 1, 1),
        dt.date(2026, 4, 30),
        ma_window=3,
        category_id=groceries.id,
    )

    assert series.actual == [10.0, 20.0, 30.0, 40.0]
    # bucket 1: avg(10); bucket 2: avg(10,20); bucket 3: avg(10,20,30); bucket 4: avg(20,30,40)
    assert series.actual_moving_avg == [10.0, 15.0, 20.0, 30.0]


def test_unknown_granularity_raises(session):
    with pytest.raises(ValueError):
        expense_vs_forecast(session, "Yearly", dt.date(2026, 1, 1), dt.date(2026, 1, 31))
