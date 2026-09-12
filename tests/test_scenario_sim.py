import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Account, Base
from app.scenario_sim import run_scenario


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _plain_account(session, name="Checking", balance=10000.0) -> Account:
    today = dt.date.today()
    account = Account(name=name, current_balance=balance, balance_as_of=today)
    session.add(account)
    session.commit()
    return account


def test_no_uncertainty_sources_gives_zero_variance_bands(session):
    account = _plain_account(session)

    result = run_scenario(
        session,
        horizon_years=2,
        account_ids=[account.id],
        n_paths=100,
        use_market_regimes=False,
        shock_probability_per_year=0.0,
        seed=1,
    )

    bands = result["baseline"]
    assert bands[5] == bands[50] == bands[95]
    assert result["regime_accounts_used"] == []


def test_guaranteed_monthly_shock_reduces_balance_deterministically(session):
    account = _plain_account(session)

    result = run_scenario(
        session,
        horizon_years=1,
        account_ids=[account.id],
        n_paths=50,
        use_market_regimes=False,
        shock_probability_per_year=12.0,  # 12/12 = 100% chance every month
        shock_amount=100.0,
        shock_account_id=account.id,
        seed=7,
    )

    bands = result["baseline"]
    # 100% monthly hit rate is deterministic even though "random" — every
    # path takes the same 12 hits, so there's still zero variance.
    assert bands[5] == bands[95]
    assert bands[50][0] == pytest.approx(account.current_balance)
    assert bands[50][-1] == pytest.approx(account.current_balance - 100.0 * 12)


def test_zero_shock_probability_matches_no_shock_baseline(session):
    account = _plain_account(session)

    with_zero_shock = run_scenario(
        session,
        horizon_years=1,
        account_ids=[account.id],
        n_paths=50,
        use_market_regimes=False,
        shock_probability_per_year=0.0,
        shock_amount=500.0,
        seed=3,
    )
    without_shock_config = run_scenario(
        session,
        horizon_years=1,
        account_ids=[account.id],
        n_paths=50,
        use_market_regimes=False,
        seed=3,
    )

    assert with_zero_shock["baseline"][50] == without_shock_config["baseline"][50]


def test_regime_delta_is_zero_without_holdings(session):
    account = _plain_account(session)

    result = run_scenario(
        session,
        horizon_years=1,
        account_ids=[account.id],
        n_paths=50,
        use_market_regimes=True,  # requested, but this account has no holdings
        seed=5,
    )

    assert result["regime_accounts_used"] == []
    bands = result["baseline"]
    assert bands[5] == bands[95]


def test_earlier_one_time_payment_costs_more_than_a_later_one(session):
    """On a growth-bearing account, paying earlier forfeits more compounding
    than paying later — so the earlier offset should show a bigger (more
    negative) impact vs. baseline."""
    account = Account(
        name="Savings", current_balance=100000.0, balance_as_of=dt.date.today(), growth_rate=10.0
    )
    session.add(account)
    session.commit()

    result = run_scenario(
        session,
        horizon_years=5,
        account_ids=[account.id],
        n_paths=20,
        use_market_regimes=False,
        one_time_payment={"amount": 1000.0, "account_id": account.id, "year_offsets": [1, 4]},
        seed=2,
    )

    impact = result["summary"]["payment_impact"]
    assert impact[1]["vs_baseline_median"] < impact[4]["vs_baseline_median"] < 0
    assert impact[1]["median_ending_balance"] < impact[4]["median_ending_balance"]


def test_shock_account_id_must_be_in_scope(session):
    account = _plain_account(session)
    other = _plain_account(session, name="Other")

    with pytest.raises(ValueError, match="shock_account_id"):
        run_scenario(
            session,
            horizon_years=1,
            account_ids=[account.id],
            shock_probability_per_year=1.0,
            shock_amount=10.0,
            shock_account_id=other.id,
        )


def test_payment_offset_beyond_horizon_raises(session):
    account = _plain_account(session)

    with pytest.raises(ValueError, match="beyond the"):
        run_scenario(
            session,
            horizon_years=2,
            account_ids=[account.id],
            one_time_payment={"amount": 100.0, "account_id": account.id, "year_offsets": [5]},
        )


def test_no_accounts_raises(session):
    with pytest.raises(ValueError, match="No accounts"):
        run_scenario(session, horizon_years=1, account_ids=[])
