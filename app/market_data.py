"""Historical price/return data for investment-account portfolios, sourced
from Yahoo Finance via yfinance. All functions fail soft (return [] / None)
on network or data errors so a missing internet connection degrades the
forecast/simulation rather than crashing the app."""

import datetime as dt

import pandas as pd
import yfinance as yf

_CACHE: dict[tuple, list[tuple[dt.date, float]]] = {}


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values()) or 1.0
    return {ticker: weight / total for ticker, weight in weights.items()}


def clear_cache() -> None:
    _CACHE.clear()


def fetch_portfolio_monthly_return_series(
    weights: dict[str, float], as_of: dt.date, lookback_years: int = 10, force_refresh: bool = False
) -> list[tuple[dt.date, float]]:
    """[(period_end_date, return)] of `weights` (ticker -> relative weight,
    renormalised to sum to 1), assuming monthly rebalancing, over the
    `lookback_years` ending at `as_of`, oldest first. Cached per (weights,
    as_of, lookback_years) since each call is a network round-trip."""
    if not weights:
        return []
    key = (tuple(sorted(weights.items())), as_of.isoformat(), lookback_years)
    if not force_refresh and key in _CACHE:
        return _CACHE[key]

    tickers = sorted(weights.keys())
    start = as_of.replace(year=as_of.year - lookback_years)
    try:
        data = yf.download(tickers, start=start, end=as_of, interval="1mo", progress=False, auto_adjust=True)
        if data.empty:
            return []
        close = data["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame(tickers[0])
        close = close[tickers].dropna()
        if len(close) < 2:
            return []
        norm_weights = _normalize_weights(weights)
        period_returns = close.pct_change().dropna()
        portfolio = sum(period_returns[t] * norm_weights[t] for t in tickers)
        series = [(idx.date(), float(r)) for idx, r in portfolio.items()]
    except Exception:
        return []

    _CACHE[key] = series
    return series


def fetch_portfolio_monthly_returns(
    weights: dict[str, float], as_of: dt.date, lookback_years: int = 10, force_refresh: bool = False
) -> list[float]:
    """Historical monthly returns only (see fetch_portfolio_monthly_return_series
    for the dated version, used by realised-historical-scenario replay)."""
    series = fetch_portfolio_monthly_return_series(weights, as_of, lookback_years, force_refresh)
    return [r for _, r in series]


def expected_monthly_return(
    weights: dict[str, float], as_of: dt.date, lookback_years: int = 10
) -> float | None:
    """Mean historical monthly return, used as the single deterministic
    growth rate for an investment account in the cashflow forecast."""
    returns = fetch_portfolio_monthly_returns(weights, as_of, lookback_years)
    if not returns:
        return None
    return sum(returns) / len(returns)
