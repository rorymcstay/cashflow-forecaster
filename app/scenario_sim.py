"""Monte Carlo stress-testing on top of the deterministic cashflow forecast.

Layers three optional, independently-toggleable sources of uncertainty onto
`app.forecast.combined_daily_forecast`'s deterministic baseline, each reusing
already-tested code rather than reimplementing the forecast stochastically:

- Market-regime variation: for accounts with investment holdings, replaces
  the deterministic mean-historical-return assumption with per-path
  historical-bootstrap resampling (`app.investment_sim.bootstrap_paths`).
- Random shocks: a Bernoulli-per-month chance of a fixed-size unplanned
  expense, independently drawn per simulated path. Modeled as a flat,
  non-compounding subtraction (not routed back through the growth engine) —
  a reasonable simplification since shocks typically hit everyday spending
  accounts, which don't carry a growth rate, and recomputing the full
  deterministic forecast per-path would be far too slow for a snappy UI.
- A one-time lump-sum payment, compared across several candidate timings
  (e.g. "pay £150k at year 1 vs 2 vs 3..."). Unlike shocks, this one *is*
  routed through the real growth engine (as a transient `OneOffEvent`) so
  it correctly forfeits whatever compounding it would have earned had it
  stayed — that's the whole point of comparing timings. Each offset reuses
  the same random shock/regime draws, so the comparison isolates the effect
  of timing alone.

Income growth and hypothetical (never-persisted) budget lines are handled
deterministically by `app.forecast` itself (`income_growth_rate`/
`extra_items` on `combined_daily_forecast`) — this module just passes them
through to the baseline call.
"""

import datetime as dt

import numpy as np
import polars as pl
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from app import investment_sim, market_data
from app.forecast import HypotheticalItem, OneOffEvent, combined_daily_forecast
from app.models import Account


def _extract_at_dates(df: pl.DataFrame, column: str, dates: list[dt.date]) -> list[float]:
    """Value of `column` at each of `dates`, clamping to the nearest
    available date for anything outside the dataframe's own range (e.g. a
    checkpoint before every in-scope account's balance_as_of)."""
    if df.height == 0 or column not in df.columns:
        return [0.0] * len(dates)
    lookup = dict(zip(df["date"].to_list(), df[column].to_list(), strict=True))
    available = sorted(lookup)
    result = []
    for d in dates:
        if d in lookup:
            result.append(lookup[d])
        elif d < available[0]:
            result.append(lookup[available[0]])
        else:
            result.append(lookup[available[-1]])
    return result


def _regime_delta(
    baseline_df: pl.DataFrame,
    dates: list[dt.date],
    accounts: list[Account],
    n_paths: int,
    lookback_years: int,
    seed: int | None,
) -> tuple[np.ndarray, list[str]]:
    """(n_paths, len(dates)) delta vs. `baseline_df`'s own deterministic
    growth assumption, summed across every in-scope account with holdings —
    zero (and account not listed) wherever historical return data isn't
    available (no network, unrecognised ticker, etc.), so callers degrade
    gracefully instead of crashing."""
    delta = np.zeros((n_paths, len(dates)))
    used: list[str] = []
    for account in accounts:
        weights = account.portfolio_weights
        if not weights:
            continue
        returns = market_data.fetch_portfolio_monthly_returns(weights, dt.date.today(), lookback_years)
        if not returns:
            continue
        paths = investment_sim.bootstrap_paths(
            returns, account.current_balance, len(dates) - 1, n_paths=n_paths, seed=seed
        )
        deterministic = np.array(_extract_at_dates(baseline_df, account.name, dates))
        delta += paths - deterministic[np.newaxis, :]
        used.append(account.name)
    return delta, used


def run_scenario(
    session: Session,
    horizon_years: int,
    account_ids: list[int] | None = None,
    n_paths: int = 500,
    income_growth_rate_pct: float = 0.0,
    shock_probability_per_year: float = 0.0,
    shock_amount: float = 0.0,
    shock_account_id: int | None = None,
    extra_budget_items: list[HypotheticalItem] | None = None,
    one_time_payment: dict | None = None,
    use_market_regimes: bool = True,
    lookback_years: int = 10,
    seed: int | None = None,
) -> dict:
    """Run a stress-tested cashflow forecast. See module docstring for the
    decomposition of uncertainty sources.

    `one_time_payment`, if given, is
    `{"amount": float, "account_id": int, "year_offsets": list[int]}` — each
    offset is compared using the same random shock/regime draws, so the
    comparison isolates the effect of payment *timing* from simulation
    noise.

    Returns:
        {
            "horizon_years", "n_paths", "dates" (ISO strings, monthly,
                including today), "regime_accounts_used",
            "baseline": {5: [...], 25: [...], 50: [...], 75: [...], 95: [...]},
            "payment_scenarios": {offset_years: {same shape as baseline}, ...}
                | None,
            "summary": {
                "probability_below_zero": float,
                "median_ending_balance": float,
                "payment_impact": {offset_years: {median_ending_balance,
                    probability_below_zero, vs_baseline_median}, ...} | None,
            },
        }
    """
    if horizon_years <= 0:
        raise ValueError("horizon_years must be positive.")

    # Resolve to a concrete seed once, even if the caller didn't pass one —
    # every regime/shock draw in this call (baseline and every payment
    # offset) must share the same seed for the offset comparison to isolate
    # timing alone, but a fresh run should still look different from the
    # last one when no explicit seed was requested.
    if seed is None:
        seed = int(np.random.default_rng().integers(0, 2**31 - 1))

    accounts = (
        session.query(Account).filter(Account.id.in_(account_ids)).all()
        if account_ids is not None
        else session.query(Account).order_by(Account.name).all()
    )
    if not accounts:
        raise ValueError("No accounts to forecast.")
    account_id_set = {a.id for a in accounts}
    if shock_account_id is not None and shock_account_id not in account_id_set:
        raise ValueError("shock_account_id must be one of the in-scope accounts.")

    today = dt.date.today()
    n_periods = horizon_years * 12
    dates = [today + relativedelta(months=i) for i in range(n_periods + 1)]

    baseline_df = combined_daily_forecast(
        session, today, dates[-1], accounts, income_growth_rate_pct, extra_budget_items
    )
    baseline_series = np.array(_extract_at_dates(baseline_df, "balance", dates))

    regime_delta = np.zeros((n_paths, n_periods + 1))
    regime_accounts_used: list[str] = []
    if use_market_regimes:
        regime_delta, regime_accounts_used = _regime_delta(
            baseline_df, dates, accounts, n_paths, lookback_years, seed
        )

    # -- shock delta: independent Bernoulli draw per path per month. See
    # module docstring for why this is a flat delta rather than growth-aware.
    shock_delta = np.zeros((n_paths, n_periods + 1))
    if shock_probability_per_year > 0 and shock_amount > 0:
        rng = np.random.default_rng(seed)
        monthly_prob = shock_probability_per_year / 12
        hits = rng.random((n_paths, n_periods)) < monthly_prob
        shock_delta[:, 1:] = np.cumsum(-shock_amount * hits, axis=1)

    combined_matrix = baseline_series[np.newaxis, :] + regime_delta + shock_delta
    baseline_bands = investment_sim.percentile_bands(combined_matrix)

    payment_scenarios = None
    payment_impact = None
    if one_time_payment:
        payment_account_id = one_time_payment["account_id"]
        if payment_account_id not in account_id_set:
            raise ValueError("one_time_payment.account_id must be one of the in-scope accounts.")
        amount = float(one_time_payment["amount"])
        baseline_median_ending = float(np.median(combined_matrix[:, -1]))

        payment_scenarios = {}
        payment_impact = {}
        for offset in one_time_payment["year_offsets"]:
            month_index = offset * 12
            if month_index > n_periods:
                raise ValueError(f"year_offset {offset} is beyond the {horizon_years}-year horizon.")

            # Routed through the real growth engine (unlike shocks above) so
            # an earlier payment correctly forfeits more compounding than a
            # later one — that's the whole point of comparing timings.
            one_off = [
                OneOffEvent(
                    date=dates[month_index],
                    amount=-amount,
                    description="Hypothetical one-time payment",
                    account_id=payment_account_id,
                )
            ]
            offset_baseline_df = combined_daily_forecast(
                session,
                today,
                dates[-1],
                accounts,
                income_growth_rate_pct,
                extra_budget_items,
                one_off,
            )
            offset_baseline_series = np.array(_extract_at_dates(offset_baseline_df, "balance", dates))
            offset_regime_delta = np.zeros((n_paths, n_periods + 1))
            if use_market_regimes:
                offset_regime_delta, _ = _regime_delta(
                    offset_baseline_df, dates, accounts, n_paths, lookback_years, seed
                )
            matrix = offset_baseline_series[np.newaxis, :] + offset_regime_delta + shock_delta

            ending = matrix[:, -1]
            payment_scenarios[offset] = investment_sim.percentile_bands(matrix)
            payment_impact[offset] = {
                "median_ending_balance": float(np.median(ending)),
                "probability_below_zero": float((matrix.min(axis=1) < 0).mean()),
                "vs_baseline_median": float(np.median(ending) - baseline_median_ending),
            }

    return {
        "horizon_years": horizon_years,
        "n_paths": n_paths,
        "dates": [d.isoformat() for d in dates],
        "regime_accounts_used": regime_accounts_used,
        "baseline": baseline_bands,
        "payment_scenarios": payment_scenarios,
        "summary": {
            "probability_below_zero": float((combined_matrix.min(axis=1) < 0).mean()),
            "median_ending_balance": float(np.median(combined_matrix[:, -1])),
            "payment_impact": payment_impact,
        },
    }
