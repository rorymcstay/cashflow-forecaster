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
    # URBAN JUNGLE classifies as "Insurance" — deliberately not a lifestyle
    # category (Eating out/Subscriptions), which get pooled instead; see
    # test_lifestyle_category_vendors_are_pooled_into_one_line_per_category.
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 10), "URBAN JUNGLE INS 111111", -9.99),
            _tx(statement, dt.date(2026, 2, 10), "URBAN JUNGLE INS 222222", -9.99),
            _tx(statement, dt.date(2026, 3, 10), "URBAN JUNGLE INS 333333", -9.99),
        ]
    )
    session.commit()

    recs = recommend_budget(session, lookback_days=365, account_ids=[account.id])

    assert len(recs) == 1
    line = recs[0]
    assert line.vendor_keys == ["URBAN JUNGLE INS"]
    assert line.amount == pytest.approx(9.99)
    assert line.frequency == Frequency.MONTHLY
    assert line.flow_type == FlowType.EXPENSE
    assert line.account_id == account.id
    assert "High-confidence" in line.rationale


def test_lifestyle_category_vendors_are_pooled_into_one_line_per_category(session):
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            # Eating out: a frequently-visited coffee shop (would otherwise
            # qualify as its own high-confidence recurring line) plus a
            # one-off pub visit — both should collapse into one pooled line.
            _tx(statement, dt.date(2026, 1, 5), "COSTA COFFEE 111111", -4.5),
            _tx(statement, dt.date(2026, 2, 5), "COSTA COFFEE 222222", -4.5),
            _tx(statement, dt.date(2026, 3, 5), "COSTA COFFEE 333333", -4.5),
            _tx(statement, dt.date(2026, 1, 20), "THE KINGS ARMS", -25.0),
            # Subscriptions: a clean recurring one plus a one-off.
            _tx(statement, dt.date(2026, 1, 10), "NETFLIX.COM 111111", -9.99),
            _tx(statement, dt.date(2026, 2, 10), "NETFLIX.COM 222222", -9.99),
            _tx(statement, dt.date(2026, 3, 10), "NETFLIX.COM 333333", -9.99),
            _tx(statement, dt.date(2026, 1, 15), "SPOTIFY PREMIUM", -10.99),
        ]
    )
    session.commit()

    recs = recommend_budget(session, lookback_days=365, account_ids=[account.id])

    by_category = {line.category_name: line for line in recs}
    assert set(by_category) == {"Eating out", "Subscriptions"}

    eating_out = by_category["Eating out"]
    assert eating_out.description == "Eating out (pooled)"
    assert set(eating_out.vendor_keys) == {"COSTA COFFEE", "THE KINGS ARMS"}

    subscriptions = by_category["Subscriptions"]
    assert subscriptions.description == "Subscriptions (pooled)"
    assert set(subscriptions.vendor_keys) == {"NETFLIX COM", "SPOTIFY PREMIUM"}


def test_standing_order_looking_lifestyle_vendor_is_kept_individual(session):
    # Matches the "Subscriptions" keyword bank (NETFLIX) but also looks like
    # a standing order — must stay an individual line, not get pooled.
    account = Account(name="Current")
    session.add(account)
    session.flush()
    statement = _make_statement(session, account, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    session.add_all(
        [
            _tx(statement, dt.date(2026, 1, 10), "NETFLIX STANDING ORDER", -9.99),
            _tx(statement, dt.date(2026, 2, 10), "NETFLIX STANDING ORDER", -9.99),
            _tx(statement, dt.date(2026, 3, 10), "NETFLIX STANDING ORDER", -9.99),
        ]
    )
    session.commit()

    recs = recommend_budget(session, lookback_days=365, account_ids=[account.id])

    assert len(recs) == 1
    line = recs[0]
    assert line.vendor_keys == ["NETFLIX STANDING ORDER"]
    assert "pooled" not in line.description
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
