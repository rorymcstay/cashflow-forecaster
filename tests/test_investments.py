import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import investments
from app.models import Account, Base, Holding


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def long_lived_session():
    """Mirrors app.db's real sessionmaker config (expire_on_commit=False) —
    the default `session` fixture's plain Session(engine) expires and
    reloads objects on every commit, which would mask the doubling bug
    regression-tested below (it only manifests when the ORM never refreshes
    stale in-memory collections after commit, as the app's long-lived
    per-process session never does)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    yield s
    s.close()


def _account_with_holdings(session, **kwargs) -> Account:
    account = Account(name="Trading 212", **kwargs)
    session.add(account)
    session.commit()
    return account


def test_portfolio_weights_derives_from_quantity_times_price(session):
    account = _account_with_holdings(session)
    session.add(Holding(account=account, ticker="AAA", quantity=10))
    session.add(Holding(account=account, ticker="BBB", quantity=5))
    session.commit()

    weights = investments.portfolio_weights(account, prices={"AAA": 10.0, "BBB": 20.0})

    # AAA: 10*10=100, BBB: 5*20=100 -> equal weights
    assert weights == {"AAA": 0.5, "BBB": 0.5}


def test_portfolio_weights_drops_unpriced_ticker(session):
    account = _account_with_holdings(session)
    session.add(Holding(account=account, ticker="AAA", quantity=10))
    session.add(Holding(account=account, ticker="BBB", quantity=5))
    session.commit()

    weights = investments.portfolio_weights(account, prices={"AAA": 10.0})

    assert weights == {"AAA": 1.0}


def test_portfolio_weights_empty_when_no_holdings(session):
    account = _account_with_holdings(session)
    assert investments.portfolio_weights(account) == {}


def test_holdings_market_value_zero_with_no_holdings(session):
    account = _account_with_holdings(session)
    assert investments.holdings_market_value(account) == 0.0


def test_holdings_market_value_none_when_unpriceable(session):
    account = _account_with_holdings(session)
    session.add(Holding(account=account, ticker="AAA", quantity=10))
    session.commit()

    assert investments.holdings_market_value(account, prices={}) is None


def test_holdings_market_value_sums_quantity_times_price(session):
    account = _account_with_holdings(session)
    session.add(Holding(account=account, ticker="AAA", quantity=10))
    session.add(Holding(account=account, ticker="BBB", quantity=5))
    session.commit()

    value = investments.holdings_market_value(account, prices={"AAA": 10.0, "BBB": 20.0})

    assert value == 200.0


def test_refresh_investment_value_sets_balance_and_bumps_as_of(session, monkeypatch):
    account = _account_with_holdings(
        session, cash_position=50.0, current_balance=0.0, balance_as_of=dt.date(2020, 1, 1)
    )
    session.add(Holding(account=account, ticker="AAA", quantity=10))
    session.commit()

    monkeypatch.setattr(investments.market_data, "fetch_latest_prices", lambda tickers: {"AAA": 10.0})

    new_balance = investments.refresh_investment_value(session, account)

    assert new_balance == 150.0  # 50 cash + 10*10 shares
    assert account.current_balance == 150.0
    assert account.balance_as_of == dt.date.today()


def test_refresh_investment_value_leaves_balance_when_price_unavailable(session, monkeypatch):
    account = _account_with_holdings(
        session, cash_position=50.0, current_balance=999.0, balance_as_of=dt.date(2020, 1, 1)
    )
    session.add(Holding(account=account, ticker="AAA", quantity=10))
    session.commit()

    monkeypatch.setattr(investments.market_data, "fetch_latest_prices", lambda tickers: {})

    result = investments.refresh_investment_value(session, account)

    assert result is None
    assert account.current_balance == 999.0
    assert account.balance_as_of == dt.date(2020, 1, 1)


def test_holdings_detail_empty_with_no_holdings(session):
    account = _account_with_holdings(session)
    assert investments.holdings_detail(account) == []


def test_holdings_detail_computes_cost_basis_and_gain(session):
    account = _account_with_holdings(session)
    session.add(Holding(account=account, ticker="AAA", quantity=10, average_price=8.0))
    session.commit()

    detail = investments.holdings_detail(account, prices={"AAA": 10.0})

    assert detail == [
        {
            "ticker": "AAA",
            "quantity": 10.0,
            "average_price": 8.0,
            "price": 10.0,
            "cost_basis": 80.0,
            "value": 100.0,
            "unrealized_gain": 20.0,
            "unrealized_gain_pct": 0.25,
        }
    ]


def test_holdings_detail_none_fields_without_average_price(session):
    account = _account_with_holdings(session)
    session.add(Holding(account=account, ticker="AAA", quantity=10))
    session.commit()

    detail = investments.holdings_detail(account, prices={"AAA": 10.0})

    assert detail[0]["average_price"] is None
    assert detail[0]["cost_basis"] is None
    assert detail[0]["unrealized_gain"] is None
    assert detail[0]["unrealized_gain_pct"] is None
    assert detail[0]["value"] == 100.0


def test_holdings_detail_none_price_fields_when_unpriceable(session):
    account = _account_with_holdings(session)
    session.add(Holding(account=account, ticker="AAA", quantity=10, average_price=8.0))
    session.commit()

    detail = investments.holdings_detail(account, prices={})

    assert detail[0]["price"] is None
    assert detail[0]["value"] is None
    assert detail[0]["cost_basis"] == 80.0
    assert detail[0]["unrealized_gain"] is None


def test_replacing_holdings_via_clear_does_not_double_on_repeated_saves(long_lived_session):
    """Regression test: editing an account's holdings (clear + re-add, the
    pattern used by AccountDialog.on_accept, the web accounts page's save(),
    and mcp_server.set_holdings) must not accumulate stale duplicates across
    repeated saves on the same long-lived session — see the module docstring
    on `long_lived_session` for why expire_on_commit=False is essential to
    reproducing this."""
    session = long_lived_session
    account = Account(name="Trading 212", current_balance=100.0)
    session.add(account)
    session.add(Holding(account=account, ticker="AAA", quantity=10))
    session.commit()

    def replace_holdings(quantity: float) -> None:
        # The correct pattern: collection.clear(), not a session.delete() loop.
        account.holdings.clear()
        session.add(Holding(account=account, ticker="AAA", quantity=quantity))
        session.commit()

    for _ in range(3):
        replace_holdings(10)

    assert len(account.holdings) == 1
    assert account.holdings[0].quantity == 10

    session.expire_all()
    assert len(account.holdings) == 1
    assert account.holdings[0].quantity == 10
