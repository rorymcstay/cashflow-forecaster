"""Historical-bootstrap Monte Carlo simulation for investment accounts.

Rather than assuming a parametric (e.g. lognormal) return distribution, each
simulated path resamples-with-replacement from an account's actual historical
monthly returns (see app.market_data) — preserving whatever skew/fat-tails
real markets showed. A "return shift" / "vol scale" affine transform lets a
scenario grid explore hypothetical conditions while keeping that real shape:

    adjusted_return = mean + (sampled_return - mean) * vol_scale + return_shift
"""

import datetime as dt

import numpy as np


def max_drawdown(path: list[float] | np.ndarray) -> float:
    """Largest peak-to-trough decline along a single value path, as a
    negative fraction (e.g. -0.35 = -35%). Computed on the raw balance, so
    regular contributions during a downturn will partly mask the underlying
    market decline — this answers "how far did the balance itself fall",
    not a pure contribution-free return drawdown."""
    values = np.asarray(path, dtype=float)
    running_max = np.maximum.accumulate(values)
    drawdown = np.divide(values - running_max, running_max, out=np.zeros_like(values), where=running_max != 0)
    return float(drawdown.min())


def drawdown_stats(paths: np.ndarray, percentiles: tuple[int, ...] = (5, 25, 50, 75, 95)) -> dict:
    """Percentile summary of every simulated path's own max drawdown (each
    path's worst peak-to-trough decline, before those are compared to each
    other) — "how bad could the ride have gotten", separate from where you
    end up."""
    paths = np.asarray(paths, dtype=float)
    running_max = np.maximum.accumulate(paths, axis=1)
    drawdowns = np.divide(paths - running_max, running_max, out=np.zeros_like(paths), where=running_max != 0)
    max_dd_per_path = drawdowns.min(axis=1)
    return {
        "worst": float(max_dd_per_path.min()),
        "best": float(max_dd_per_path.max()),
        "mean": float(max_dd_per_path.mean()),
        "percentiles": {int(p): float(np.percentile(max_dd_per_path, p)) for p in percentiles},
    }


def realised_historical_scenarios(
    return_series: list[tuple[dt.date, float]],
    initial_value: float,
    n_periods: int,
    monthly_contribution: float = 0.0,
) -> list[dict]:
    """Every actual, non-random n_periods-long window of `return_series`
    (oldest first) replayed in real chronological order — "if you'd started
    investing on date X, here's what would really have happened" — for every
    X the historical data allows. Unlike bootstrap_paths, nothing is resampled
    or shuffled: each scenario is a real sequence of events that occurred.
    """
    scenarios = []
    for start in range(len(return_series) - n_periods + 1):
        window = return_series[start : start + n_periods]
        value = initial_value
        trajectory = [value]
        for _, r in window:
            value = value * (1 + r) + monthly_contribution
            trajectory.append(value)
        scenarios.append(
            {
                "start_date": window[0][0].isoformat(),
                "end_date": window[-1][0].isoformat(),
                "ending_balance": trajectory[-1],
                "max_drawdown": max_drawdown(trajectory),
                "trajectory": trajectory,
            }
        )
    return scenarios


def bootstrap_paths(
    historical_returns: list[float],
    initial_value: float,
    n_periods: int,
    n_paths: int = 1000,
    monthly_contribution: float = 0.0,
    return_shift: float = 0.0,
    vol_scale: float = 1.0,
    seed: int | None = None,
) -> np.ndarray:
    """(n_paths, n_periods + 1) array of simulated account values, column 0
    being `initial_value`. Contributions are added at the end of each period,
    after that period's return is applied."""
    if n_periods <= 0:
        return np.full((max(n_paths, 1), 1), initial_value)
    if not historical_returns:
        return np.full((max(n_paths, 1), n_periods + 1), initial_value)

    rng = np.random.default_rng(seed)
    hist = np.array(historical_returns, dtype=float)
    mean = hist.mean()

    paths = np.empty((n_paths, n_periods + 1))
    values = np.full(n_paths, initial_value, dtype=float)
    paths[:, 0] = values
    for t in range(n_periods):
        sampled = rng.choice(hist, size=n_paths)
        adjusted = mean + (sampled - mean) * vol_scale + return_shift
        values = values * (1 + adjusted) + monthly_contribution
        paths[:, t + 1] = values
    return paths


def percentile_bands(
    paths: np.ndarray, percentiles: tuple[int, ...] = (5, 25, 50, 75, 95)
) -> dict[int, list[float]]:
    """{percentile: [value at each period]} across all simulated paths."""
    return {p: np.percentile(paths, p, axis=0).tolist() for p in percentiles}


def _cell_stats(paths: np.ndarray) -> dict:
    return {
        "median_ending_balance": float(np.median(paths[:, -1])),
        "median_max_drawdown": drawdown_stats(paths)["percentiles"][50],
    }


def return_vol_grid(
    historical_returns: list[float],
    initial_value: float,
    n_periods: int,
    return_shifts: list[float],
    vol_scales: list[float],
    n_paths: int = 500,
    monthly_contribution: float = 0.0,
    seed: int | None = None,
) -> dict[tuple[float, float], dict]:
    """{(return_shift, vol_scale): {median_ending_balance, median_max_drawdown}}
    for every combination — "what if the market's average return/volatility
    were different?"."""
    results = {}
    for rs in return_shifts:
        for vs in vol_scales:
            paths = bootstrap_paths(
                historical_returns,
                initial_value,
                n_periods,
                n_paths=n_paths,
                monthly_contribution=monthly_contribution,
                return_shift=rs,
                vol_scale=vs,
                seed=seed,
            )
            results[(rs, vs)] = _cell_stats(paths)
    return results


def contribution_horizon_grid(
    historical_returns: list[float],
    initial_value: float,
    monthly_contributions: list[float],
    horizon_years: list[int],
    n_paths: int = 500,
    seed: int | None = None,
) -> dict[tuple[float, int], dict]:
    """{(monthly_contribution, horizon_years): {median_ending_balance, median_max_drawdown}}
    for every combination, using the unadjusted historical return
    distribution — "how much do I need to save, and for how long?"."""
    results = {}
    for contribution in monthly_contributions:
        for years in horizon_years:
            paths = bootstrap_paths(
                historical_returns,
                initial_value,
                years * 12,
                n_paths=n_paths,
                monthly_contribution=contribution,
                seed=seed,
            )
            results[(contribution, years)] = _cell_stats(paths)
    return results


def grid_values(start: float, stop: float, step: float) -> list[float]:
    """Inclusive range start..stop stepped by `step`, rounded to avoid float
    drift — shared by any front end building a return/vol or
    contribution/horizon scenario grid."""
    if step <= 0 or stop < start:
        return [start]
    values = []
    v = start
    while v <= stop + 1e-9:
        values.append(round(v, 6))
        v += step
    return values
