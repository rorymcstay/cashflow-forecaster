import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.budget_recommender import recommend_budget
from app.budgets import get_active_budget
from app.models import Account, Base, BudgetItem, Category, FlowType, Frequency, Statement, Transaction


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


def test_recurring_bill_produces_high_confidence_vendor_line(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 10), "NETFLIX.COM 111111", -9.99),
            _tx(statement, dt.date(2026, 2, 10), "NETFLIX.COM 222222", -9.99),
            _tx(statement, dt.date(2026, 3, 10), "NETFLIX.COM 333333", -9.99),
        ]
    )
    session.commit()

    recs = recommend_budget(session, lookback_days=365, account_ids=[account.id])

    assert len(recs) == 1
    line = recs[0]
    assert line.vendor_keys == ["NETFLIX COM"]
    assert line.amount == pytest.approx(9.99)
    assert line.frequency == Frequency.MONTHLY
    assert line.flow_type == FlowType.EXPENSE
    assert line.account_id == account.id
    assert "High-confidence" in line.rationale


def test_already_captured_vendor_is_not_recommended_again(session):
    account = Account(name="Current")
    groceries = Category(name="Subscriptions")
    session.add_all([account, groceries])
    session.flush()
    budget = get_active_budget(session)
    session.add(
        BudgetItem(
            description="Netflix",
            amount=9.99,
            flow_type=FlowType.EXPENSE,
            frequency=Frequency.MONTHLY,
            effective_from=dt.date(2025, 1, 1),
            category=groceries,
            account=account,
            budget=budget,
        )
    )
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 10), "NETFLIX.COM 111111", -9.99),
            _tx(statement, dt.date(2026, 2, 10), "NETFLIX.COM 222222", -9.99),
            _tx(statement, dt.date(2026, 3, 10), "NETFLIX.COM 333333", -9.99),
        ]
    )
    session.commit()

    recs = recommend_budget(session, lookback_days=365, account_ids=[account.id])

    assert recs == []


def test_non_recurring_category_spend_falls_back_to_catchall(session):
    account = Account(name="Current")
    groceries = Category(name="Groceries")
    session.add_all([account, groceries])
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 5), "TESCO STORES", -60.0, category=groceries),
            _tx(statement, dt.date(2026, 1, 12), "ALDI SUPERMARKET", -40.0, category=groceries),
            _tx(statement, dt.date(2026, 1, 20), "WAITROSE", -50.0, category=groceries),
        ]
    )
    session.commit()

    recs = recommend_budget(session, lookback_days=365, account_ids=[account.id], min_monthly_amount=5.0)

    assert len(recs) == 1
    line = recs[0]
    assert line.vendor_keys == []
    assert line.category_name == "Groceries"
    # 3-month trailing average ending at the category's last transaction
    # (Jan): the two preceding months have no spend, so £150 dilutes to £50.
    assert line.amount == pytest.approx(50.0)
    assert "catch-all" in line.rationale


def test_below_materiality_threshold_produces_no_catchall_line(session):
    account = Account(name="Current")
    groceries = Category(name="Groceries")
    session.add_all([account, groceries])
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 1, 31))
    session.add(_tx(statement, dt.date(2026, 1, 5), "CORNER SHOP", -2.0, category=groceries))
    session.commit()

    recs = recommend_budget(session, lookback_days=365, account_ids=[account.id], min_monthly_amount=5.0)

    assert recs == []


def test_income_is_never_recommended(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 1), "SALARY", 2000.0),
            _tx(statement, dt.date(2026, 2, 1), "SALARY", 2000.0),
            _tx(statement, dt.date(2026, 3, 1), "SALARY", 2000.0),
        ]
    )
    session.commit()

    recs = recommend_budget(session, lookback_days=365, account_ids=[account.id])

    assert recs == []
