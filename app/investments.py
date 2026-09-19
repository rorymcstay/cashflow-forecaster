"""Reconciling an investment account's holdings (share quantities, priced
live) and un-invested cash_position into its current_balance, and deriving
the {ticker: relative weight} shape the forecast/simulation math (see
app/forecast.py, app/investment_sim.py, app/scenario_sim.py) already expects
— that math still thinks in terms of a fixed weight mix over the lookback
period, just sourced from quantity x price now instead of being stored
directly.
"""

import datetime as dt

from sqlalchemy.orm import Session

from app import market_data
from app.models import Account


def portfolio_weights(account: Account, prices: dict[str, float] | None = None) -> dict[str, float]:
    """{ticker: relative weight}, derived from each held ticker's live price.
    A ticker whose price can't be fetched drops out of the mix (its
    quantity contributes nothing) rather than raising. Empty if the account
    has no holdings, or if none of its holdings' prices could be fetched."""
    quantities = account.share_quantities
    if not quantities:
        return {}
    prices = prices if prices is not None else market_data.fetch_latest_prices(list(quantities))
    values = {t: q * prices[t] for t, q in quantities.items() if prices.get(t)}
    if not values:
        return {}
    total = sum(values.values())
    return {t: v / total for t, v in values.items()}


def holdings_market_value(account: Account, prices: dict[str, float] | None = None) -> float | None:
    """Current £ value of the account's share holdings (quantity x live
    price). 0.0 if it has no holdings at all; None if it has holdings but
    none of their prices could be fetched (network failure) — kept distinct
    from a genuine zero so callers don't overwrite a real balance with 0 on
    a bad network call."""
    quantities = account.share_quantities
    if not quantities:
        return 0.0
    prices = prices if prices is not None else market_data.fetch_latest_prices(list(quantities))
    priced = {t: q * prices[t] for t, q in quantities.items() if prices.get(t)}
    if not priced:
        return None
    return sum(priced.values())


def holdings_detail(account: Account, prices: dict[str, float] | None = None) -> list[dict]:
    """Per-holding breakdown against live prices: quantity, cost basis
    (average_price x quantity, None if average_price was never set), market
    value, and unrealized gain/loss in £ and % — the "did this actually make
    money" report a flat weight/return projection can't give you, since that
    only ever describes the ticker's market performance, not what you
    personally paid. price/value/unrealized_* are None for a ticker whose
    price couldn't be fetched, same fail-soft convention as the rest of this
    module."""
    if not account.holdings:
        return []
    prices = prices if prices is not None else market_data.fetch_latest_prices(list(account.share_quantities))
    rows = []
    for h in account.holdings:
        price = prices.get(h.ticker)
        value = h.quantity * price if price is not None else None
        cost_basis = h.quantity * h.average_price if h.average_price is not None else None
        gain = value - cost_basis if value is not None and cost_basis is not None else None
        gain_pct = gain / cost_basis if gain is not None and cost_basis else None
        rows.append(
            {
                "ticker": h.ticker,
                "quantity": h.quantity,
                "average_price": h.average_price,
                "price": price,
                "cost_basis": cost_basis,
                "value": value,
                "unrealized_gain": gain,
                "unrealized_gain_pct": gain_pct,
            }
        )
    return rows


def refresh_investment_value(
    session: Session, account: Account, prices: dict[str, float] | None = None
) -> float | None:
    """Recompute and persist current_balance = cash_position + live market
    value of holdings, bumping balance_as_of to today. Returns the new
    balance, or None (balance left untouched) if no price could be fetched.
    Pass `prices` if the caller already fetched them (e.g. alongside
    holdings_detail) to avoid a redundant round-trip.

    This is the only place current_balance is written for an account with
    holdings — direct edits to it are rejected elsewhere (see
    mcp_server.update_account) specifically so this stays the single source
    of truth once an account has holdings.
    """
    value = holdings_market_value(account, prices=prices)
    if value is None:
        return None
    account.current_balance = account.cash_position + value
    account.balance_as_of = dt.date.today()
    session.commit()
    return account.current_balance
