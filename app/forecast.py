import datetime as dt
from collections import defaultdict

import polars as pl
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from app.models import Account, BudgetItem, Frequency, FlowType, UpcomingExpense

PERIOD_DAYS = {
    Frequency.WEEKLY: 7,
    Frequency.FORTNIGHTLY: 14,
    Frequency.FOUR_WEEKLY: 28,
}
PERIOD_MONTHS = {
    Frequency.MONTHLY: 1,
    Frequency.QUARTERLY: 3,
    Frequency.SIX_MONTHLY: 6,
    Frequency.ANNUALLY: 12,
}


def generate_occurrences(anchor: dt.date, until: dt.date | None, frequency: Frequency,
                          range_start: dt.date, range_end: dt.date) -> list[dt.date]:
    """All occurrence dates of a recurring item that fall within [range_start, range_end]."""
    effective_end = range_end if until is None else min(range_end, until)
    if effective_end < range_start or anchor > effective_end:
        return []

    occurrences: list[dt.date] = []
    if frequency in PERIOD_DAYS:
        step = PERIOD_DAYS[frequency]
        if anchor >= range_start:
            current = anchor
        else:
            days_since = (range_start - anchor).days
            offset = (-days_since) % step
            current = range_start + dt.timedelta(days=offset)
        while current <= effective_end:
            if current >= anchor:
                occurrences.append(current)
            current += dt.timedelta(days=step)
    else:
        months = PERIOD_MONTHS[frequency]
        k = 0
        current = anchor
        while current < range_start:
            k += months
            current = anchor + relativedelta(months=k)
        while current <= effective_end:
            occurrences.append(current)
            k += months
            current = anchor + relativedelta(months=k)
    return occurrences


def monthly_budget_summary(session: Session, as_of: dt.date | None = None) -> pl.DataFrame:
    """Monthly-equivalent totals per (flow_type, category) for items active on `as_of`."""
    as_of = as_of or dt.date.today()
    items = session.query(BudgetItem).all()
    rows = [
        {
            "flow_type": item.flow_type.value,
            "category": item.category.name,
            "monthly_amount": item.monthly_equivalent,
        }
        for item in items if item.is_active_on(as_of)
    ]
    if not rows:
        return pl.DataFrame(schema={"flow_type": pl.Utf8, "category": pl.Utf8, "monthly_amount": pl.Float64})
    df = pl.DataFrame(rows)
    return (df.group_by(["flow_type", "category"])
              .agg(pl.col("monthly_amount").sum())
              .sort(["flow_type", "category"]))


_EMPTY_FORECAST_SCHEMA = {
    "date": pl.Date, "in": pl.Float64, "out": pl.Float64, "net": pl.Float64,
    "balance": pl.Float64, "below_threshold": pl.Boolean, "details": pl.Utf8,
}


def _collect_account_events(session: Session, account: Account,
                             compute_start: dt.date, compute_end: dt.date) -> list[tuple[dt.date, float, str]]:
    """(date, signed_amount, description) for every occurrence of this account's
    budget items and upcoming expenses within [compute_start, compute_end]."""
    events: list[tuple[dt.date, float, str]] = []
    for item in session.query(BudgetItem).filter_by(account_id=account.id).all():
        signed = item.amount if item.flow_type == FlowType.INCOME else -item.amount
        for occ in generate_occurrences(item.effective_from, item.effective_until, item.frequency,
                                         compute_start, compute_end):
            events.append((occ, signed, item.description))

    for exp in (session.query(UpcomingExpense)
                .filter(UpcomingExpense.account_id == account.id)
                .filter(UpcomingExpense.date >= compute_start)
                .filter(UpcomingExpense.date <= compute_end)):
        events.append((exp.date, -exp.amount, exp.description))

    return events


def _details_by_date(events: list[tuple[dt.date, float, str]]) -> dict[dt.date, str]:
    grouped: dict[dt.date, list[str]] = defaultdict(list)
    for occ, signed, description in events:
        sign = "+" if signed > 0 else "-"
        grouped[occ].append(f"{description} ({sign}£{abs(signed):,.2f})")
    return {d: "; ".join(items) for d, items in grouped.items()}


def account_daily_forecast(session: Session, account: Account,
                            range_start: dt.date, range_end: dt.date) -> pl.DataFrame:
    """Daily in/out/net/balance/details for one account, sliced to [range_start, range_end].

    Internally the running balance is computed forward from account.balance_as_of
    (which may fall before range_start) so the balance shown on range_start is
    accurate rather than reset to the opening figure. `details` lists which
    budget items / upcoming expenses contributed to that day's net figure.
    """
    if account.balance_as_of > range_end:
        return pl.DataFrame(schema=_EMPTY_FORECAST_SCHEMA)

    compute_start = account.balance_as_of
    compute_end = range_end

    events = _collect_account_events(session, account, compute_start, compute_end)
    details_map = _details_by_date(events)

    date_series = pl.date_range(compute_start, compute_end, interval="1d", eager=True)
    base = pl.DataFrame({"date": date_series})

    if events:
        ev_df = pl.DataFrame([(d, a) for d, a, _ in events], schema=["date", "amount"], orient="row")
        in_df = (ev_df.filter(pl.col("amount") > 0).group_by("date")
                 .agg(pl.col("amount").sum().alias("in")))
        out_df = (ev_df.filter(pl.col("amount") < 0).group_by("date")
                  .agg((-pl.col("amount")).sum().alias("out")))
    else:
        in_df = pl.DataFrame(schema={"date": pl.Date, "in": pl.Float64})
        out_df = pl.DataFrame(schema={"date": pl.Date, "out": pl.Float64})

    df = (base.join(in_df, on="date", how="left")
              .join(out_df, on="date", how="left")
              .with_columns([pl.col("in").fill_null(0.0), pl.col("out").fill_null(0.0)])
              .with_columns((pl.col("in") - pl.col("out")).alias("net"))
              .sort("date"))

    first_net = df["net"][0]
    df = (df.with_columns(pl.col("net").cum_sum().alias("_cum"))
            .with_columns((pl.lit(account.current_balance) + pl.col("_cum") - pl.lit(first_net))
                          .alias("balance"))
            .drop("_cum"))

    if account.low_balance_threshold is not None:
        df = df.with_columns((pl.col("balance") < account.low_balance_threshold).alias("below_threshold"))
    else:
        df = df.with_columns(pl.lit(False).alias("below_threshold"))

    details_col = [details_map.get(d, "") for d in date_series.to_list()]
    df = df.with_columns(pl.Series("details", details_col))

    return df.filter((pl.col("date") >= range_start) & (pl.col("date") <= range_end))


def combined_daily_forecast(session: Session, range_start: dt.date, range_end: dt.date) -> pl.DataFrame:
    """Combined in/out/net/balance across every account, valid from the latest
    balance_as_of onward, plus a per-account balance breakdown and a details
    column listing every contributing item that day (prefixed by account)."""
    accounts = session.query(Account).all()
    empty_schema = {"date": pl.Date, "in": pl.Float64, "out": pl.Float64, "net": pl.Float64,
                     "balance": pl.Float64, "details": pl.Utf8}
    if not accounts:
        return pl.DataFrame(schema=empty_schema)

    combined_start = max(a.balance_as_of for a in accounts)
    effective_start = max(combined_start, range_start)
    if effective_start > range_end:
        return pl.DataFrame(schema=empty_schema)

    per_account = []
    all_details: dict[dt.date, list[str]] = defaultdict(list)
    for account in accounts:
        df = account_daily_forecast(session, account, effective_start, range_end)
        if df.height == 0:
            continue
        per_account.append(df.select(["date", "in", "out", "net", "balance"])
                              .rename({"in": f"{account.name} in", "out": f"{account.name} out",
                                       "net": f"{account.name} net", "balance": account.name}))
        for row in df.iter_rows(named=True):
            if row["details"]:
                all_details[row["date"]].append(f"{account.name}: {row['details']}")

    if not per_account:
        return pl.DataFrame(schema=empty_schema)

    merged = per_account[0]
    for df in per_account[1:]:
        merged = merged.join(df, on="date", how="left")

    balance_cols = [c for c in merged.columns if c in [a.name for a in accounts]]
    in_cols = [f"{a.name} in" for a in accounts if f"{a.name} in" in merged.columns]
    out_cols = [f"{a.name} out" for a in accounts if f"{a.name} out" in merged.columns]

    merged = merged.with_columns([
        pl.sum_horizontal(balance_cols).alias("balance"),
        pl.sum_horizontal(in_cols).alias("in"),
        pl.sum_horizontal(out_cols).alias("out"),
    ])
    merged = merged.with_columns((pl.col("in") - pl.col("out")).alias("net"))

    details_col = ["; ".join(all_details.get(d, [])) for d in merged["date"].to_list()]
    merged = merged.with_columns(pl.Series("details", details_col))

    net_cols = [f"{a.name} net" for a in accounts if f"{a.name} net" in merged.columns]
    return merged.select(["date", "in", "out", "net", "balance", "details", *balance_cols,
                           *[c for c in merged.columns if c in in_cols + out_cols + net_cols]])


def low_balance_warnings(session: Session, range_start: dt.date, range_end: dt.date) -> list[dict]:
    """Every (account, date) in the range where the forecast balance dips below threshold."""
    warnings = []
    for account in session.query(Account).all():
        if account.low_balance_threshold is None:
            continue
        df = account_daily_forecast(session, account, range_start, range_end)
        for row in df.filter(pl.col("below_threshold")).iter_rows(named=True):
            warnings.append({
                "account": account.name,
                "date": row["date"],
                "balance": row["balance"],
                "threshold": account.low_balance_threshold,
            })
    return warnings
