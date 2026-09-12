import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.forecast import HypotheticalItem, OneOffEvent, combined_daily_forecast
from app.models import Account, Base, BudgetItem, Category, FlowType, Frequency


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def category(session):
    cat = Category(name="Test")
    session.add(cat)
    session.flush()
    return cat


def test_income_growth_rate_zero_matches_default_behavior(session, category):
    account = Account(name="Checking", current_balance=0.0, balance_as_of=dt.date(2026, 1, 1))
    session.add(account)
    session.flush()
    session.add(
        BudgetItem(
            description="Salary",
            amount=1000.0,
            flow_type=FlowType.INCOME,
            frequency=Frequency.MONTHLY,
            effective_from=dt.date(2026, 1, 1),
            category=category,
            account=account,
        )
    )
    session.commit()

    end = dt.date(2027, 1, 1)
    default_df = combined_daily_forecast(session, dt.date(2026, 1, 1), end, [account])
    explicit_zero_df = combined_daily_forecast(session, dt.date(2026, 1, 1), end, [account], 0.0, None)

    assert default_df["balance"].to_list() == explicit_zero_df["balance"].to_list()
    # 12 monthly £1000 salary payments, no growth, no expenses.
    assert default_df["balance"][-1] == pytest.approx(12000.0)


def test_income_growth_escalates_income_only(session, category):
    # income_growth_rate escalates relative to the real wall-clock "today"
    # (not whatever range_start the forecast happens to query), so the test
    # anchors to it too rather than hardcoding calendar dates.
    today = dt.date.today()
    account = Account(name="Checking", current_balance=0.0, balance_as_of=today)
    session.add(account)
    session.flush()
    session.add_all(
        [
            BudgetItem(
                description="Salary",
                amount=1000.0,
                flow_type=FlowType.INCOME,
                frequency=Frequency.ANNUALLY,
                effective_from=today,
                category=category,
                account=account,
            ),
            BudgetItem(
                description="Rent",
                amount=1000.0,
                flow_type=FlowType.EXPENSE,
                frequency=Frequency.ANNUALLY,
                effective_from=today,
                category=category,
                account=account,
            ),
        ]
    )
    session.commit()

    # today's occurrences are excluded from the displayed balance (the
    # existing day-0 baseline convention — see account_daily_forecast); only
    # the +1yr occurrences count, income escalated ~10%, expense flat.
    end = today + dt.timedelta(days=400)
    df = combined_daily_forecast(session, today, end, [account], 10.0, None)

    final_balance = df["balance"][-1]
    assert final_balance == pytest.approx(1000 * 1.10 - 1000, rel=0.01)
    assert final_balance > 0  # proves income grew faster than the flat expense


def test_extra_items_affect_forecast_without_touching_db(session):
    account = Account(name="Checking", current_balance=0.0, balance_as_of=dt.date(2026, 1, 1))
    session.add(account)
    session.commit()

    hypothetical = HypotheticalItem(
        description="New gym membership",
        amount=50.0,
        flow_type=FlowType.EXPENSE,
        frequency=Frequency.MONTHLY,
        account_id=account.id,
        effective_from=dt.date(2026, 1, 1),
    )
    end = dt.date(2026, 4, 1)
    without_df = combined_daily_forecast(session, dt.date(2026, 1, 1), end, [account])
    with_df = combined_daily_forecast(session, dt.date(2026, 1, 1), end, [account], 0.0, [hypothetical])

    assert without_df["balance"][-1] == pytest.approx(0.0)
    # 4 monthly occurrences (Jan-Apr) of -£50, minus the day-0 (Jan 1)
    # occurrence excluded by account_daily_forecast's baseline convention.
    assert with_df["balance"][-1] == pytest.approx(-150.0)
    # Never persisted to the database.
    assert session.query(BudgetItem).count() == 0


def test_hypothetical_item_rejects_transfers():
    with pytest.raises(ValueError, match="transfers"):
        HypotheticalItem(
            description="Move money",
            amount=100.0,
            flow_type=FlowType.TRANSFER,
            frequency=Frequency.MONTHLY,
            account_id=1,
        )


def test_one_off_event_flows_through_growth_engine(session):
    """A OneOffEvent withdrawal should reduce the balance from its date
    onward and lose whatever growth it would otherwise have earned."""
    account = Account(
        name="Savings", current_balance=10000.0, balance_as_of=dt.date(2026, 1, 1), growth_rate=12.0
    )
    session.add(account)
    session.commit()

    end = dt.date(2027, 1, 1)
    without_df = combined_daily_forecast(session, dt.date(2026, 1, 1), end, [account])

    payment = OneOffEvent(
        date=dt.date(2026, 1, 15), amount=-1000.0, description="Withdrawal", account_id=account.id
    )
    with_df = combined_daily_forecast(session, dt.date(2026, 1, 1), end, [account], 0.0, None, [payment])

    # The withdrawal itself, PLUS a full year of foregone growth on it —
    # more than a flat £1000 difference.
    diff = without_df["balance"][-1] - with_df["balance"][-1]
    assert diff > 1000.0
