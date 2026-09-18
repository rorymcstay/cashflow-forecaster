import calendar
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

import polars as pl
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from app import market_data
from app.budgets import active_budget_items
from app.models import Account, BudgetItem, FlowType, Frequency, Statement, Transaction, UpcomingExpense

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


@dataclass
class HypotheticalItem:
    """A transient, never-persisted budget-item-shaped line for scenario
    forecasting (see app/scenario_sim.py) — same occurrence/amount semantics
    as a real BudgetItem, but it only ever exists in memory for the duration
    of one forecast call. Income/expense only: a hypothetical line is for
    "what if I added this budget item", not for modelling a transfer between
    accounts, so transfers are rejected rather than silently mishandled.
    """

    description: str
    amount: float
    flow_type: FlowType
    frequency: Frequency
    account_id: int
    effective_from: dt.date = field(default_factory=dt.date.today)
    effective_until: dt.date | None = None
    target_account: None = field(default=None, init=False)  # transfers unsupported; see __post_init__

    def __post_init__(self) -> None:
        if self.flow_type == FlowType.TRANSFER:
            raise ValueError("HypotheticalItem doesn't support transfers — income/expense only.")


@dataclass
class OneOffEvent:
    """A single, non-recurring transient (date, signed amount) charge scoped
    to one account — used for "what if I made this one-time payment on this
    date" scenario forecasting (see app/scenario_sim.py). Flows through the
    same growth-compounding math as any other event, so a payment correctly
    forfeits whatever growth it would have earned had it stayed. `amount` is
    already signed (positive = money in, negative = money out) — unlike
    HypotheticalItem/BudgetItem, there's no flow_type to derive it from."""

    date: dt.date
    amount: float
    description: str
    account_id: int


def generate_occurrences(
    anchor: dt.date, until: dt.date | None, frequency: Frequency, range_start: dt.date, range_end: dt.date
) -> list[dt.date]:
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


def monthly_budget_summary(
    session: Session, as_of: dt.date | None = None, exclude_account_ids: set[int] | list[int] | None = None
) -> pl.DataFrame:
    """Monthly-equivalent totals per (flow_type, category) for items active on
    `as_of`, excluding any billed to an account in `exclude_account_ids`."""
    as_of = as_of or dt.date.today()
    excluded = set(exclude_account_ids or ())
    items = active_budget_items(session).all()
    rows = [
        {
            "flow_type": item.flow_type.value,
            "category": item.category.name,
            "monthly_amount": item.monthly_equivalent,
        }
        for item in items
        if item.is_active_on(as_of) and item.account_id not in excluded
    ]
    if not rows:
        return pl.DataFrame(schema={"flow_type": pl.Utf8, "category": pl.Utf8, "monthly_amount": pl.Float64})
    df = pl.DataFrame(rows)
    return (
        df.group_by(["flow_type", "category"])
        .agg(pl.col("monthly_amount").sum())
        .sort(["flow_type", "category"])
    )


def monthly_savings_amount(
    session: Session, as_of: dt.date | None = None, exclude_account_ids: set[int] | list[int] | None = None
) -> float:
    """Monthly-equivalent total of recurring transfers landing in an
    investment account (growth_rate or holdings set) — the portion of
    Transfer activity that's genuinely being saved/invested, as opposed to
    just reshuffled between everyday spending accounts."""
    as_of = as_of or dt.date.today()
    excluded = set(exclude_account_ids or ())
    total = 0.0
    for item in active_budget_items(session).filter_by(flow_type=FlowType.TRANSFER).all():
        if not item.is_active_on(as_of) or item.account_id in excluded:
            continue
        target = item.target_account
        if target is not None and (target.growth_rate or target.portfolio_weights):
            total += item.monthly_equivalent
    return total


def investment_accounts_summary(session: Session, as_of: dt.date | None = None) -> list[dict]:
    """Per-investment-account (growth_rate or holdings set) snapshot: current
    balance, annualised return, and the resulting estimated monthly growth in
    £ — used by the financial health dashboard. Holdings-based accounts use
    the mean historical monthly return (same source as the cashflow
    forecast); manual accounts use their fixed growth_rate."""
    as_of = as_of or dt.date.today()
    results = []
    for account in session.query(Account).order_by(Account.name).all():
        if account.portfolio_weights:
            monthly_rate = market_data.expected_monthly_return(account.portfolio_weights, as_of)
            if monthly_rate is None:
                continue
            source = "/".join(sorted(account.portfolio_weights))
        elif account.growth_rate:
            monthly_rate = (1 + account.growth_rate / 100) ** (1 / 12) - 1
            source = f"{account.growth_rate:.2f}% APY (manual)"
        else:
            continue
        annual_rate = (1 + monthly_rate) ** 12 - 1
        results.append(
            {
                "account_id": account.id,
                "account": account.name,
                "balance": account.current_balance,
                "annual_rate": annual_rate,
                "monthly_growth_estimate": account.current_balance * monthly_rate,
                "source": source,
            }
        )
    return results


_EMPTY_FORECAST_SCHEMA = {
    "date": pl.Date,
    "in": pl.Float64,
    "out": pl.Float64,
    "net": pl.Float64,
    "balance": pl.Float64,
    "below_threshold": pl.Boolean,
    "details": pl.Utf8,
}


def _investment_growth_rates(
    account: Account, postings: list[dt.date]
) -> tuple[dict[dt.date, float], dict[dt.date, str]]:
    """Per-posting (rate, label) for an investment account: the real market
    return for any month that's already happened ("realised"), falling back
    to a flat mean-historical-return assumption for months still in the
    future ("theoretical") since those can't be known yet."""
    if not postings:
        return {}, {}
    weights = account.portfolio_weights
    tickers = "/".join(sorted(weights))

    today = dt.date.today()
    as_of_cutoff = min(postings[-1], today)
    lookback_years = max(10, postings[-1].year - postings[0].year + 2)
    series = market_data.fetch_portfolio_monthly_return_series(weights, as_of_cutoff, lookback_years)
    if not series:
        return {}, {}
    market_by_month = {(d.year, d.month): r for d, r in series}
    theoretical_rate = sum(r for _, r in series) / len(series)

    rates: dict[dt.date, float] = {}
    labels: dict[dt.date, str] = {}
    for posting in postings:
        key = (posting.year, posting.month)
        if posting <= today and key in market_by_month:
            rates[posting] = market_by_month[key]
            labels[posting] = f"Growth (realised, {tickers})"
        else:
            rates[posting] = theoretical_rate
            labels[posting] = f"Growth (theoretical, {tickers})"
    return rates, labels


def _manual_growth_rates(
    account: Account, postings: list[dt.date]
) -> tuple[dict[dt.date, float], dict[dt.date, str]]:
    monthly_rate = (1 + account.growth_rate / 100) ** (1 / 12) - 1
    label = f"Growth ({account.growth_rate:.2f}% APY)"
    return {p: monthly_rate for p in postings}, {p: label for p in postings}


def _monthly_growth_events(
    account: Account, events: list[tuple[dt.date, float, str]], compute_start: dt.date, compute_end: dt.date
) -> list[tuple[dt.date, float, str]]:
    """Growth events for any account with a growth_rate or holdings set,
    compounded monthly (posted on the last day of each month, or compute_end
    for a partial final month) on top of the running balance — principal,
    contributions and prior growth."""
    postings: list[dt.date] = []
    month_cursor = compute_start.replace(day=1)
    while month_cursor <= compute_end:
        last_day = calendar.monthrange(month_cursor.year, month_cursor.month)[1]
        posting = min(dt.date(month_cursor.year, month_cursor.month, last_day), compute_end)
        postings.append(posting)
        month_cursor += relativedelta(months=1)

    if account.portfolio_weights:
        rates, labels = _investment_growth_rates(account, postings)
    elif account.growth_rate:
        rates, labels = _manual_growth_rates(account, postings)
    else:
        return []

    amounts_by_date: dict[dt.date, float] = defaultdict(float)
    for occ, signed, _ in events:
        amounts_by_date[occ] += signed

    growth_events: list[tuple[dt.date, float, str]] = []
    running = account.current_balance
    posting_set = set(postings)
    current = compute_start
    while current <= compute_end:
        running += amounts_by_date.get(current, 0.0)
        if current in posting_set and current in rates:
            growth = running * rates[current]
            growth_events.append((current, growth, labels[current]))
            running += growth
        current += dt.timedelta(days=1)
    return growth_events


def _income_growth_multiplier(occ: dt.date, income_growth_rate: float) -> float:
    """Escalation factor for an INCOME occurrence `income_growth_rate`%
    (annual) years from today — 1.0 for anything today or in the past, since
    growth only ever projects forward, never rewrites history."""
    if not income_growth_rate:
        return 1.0
    years = (occ - dt.date.today()).days / 365.25
    return (1 + income_growth_rate / 100) ** years if years > 0 else 1.0


def _collect_own_charge_events(
    session: Session,
    account: Account,
    compute_start: dt.date,
    compute_end: dt.date,
    income_growth_rate: float = 0.0,
    extra_items: list[HypotheticalItem] | None = None,
    one_off_events: list[OneOffEvent] | None = None,
) -> list[tuple[dt.date, float, str]]:
    """(date, signed_amount, description) for budget items and upcoming
    expenses billed directly to this account — the base "charges" used both
    for its own forecast and, if it's a credit card, to size its autopay.
    Excludes incoming transfers, growth and credit-card autopay, which are
    layered on separately by `_collect_account_events`.

    `income_growth_rate` (annual %) escalates INCOME occurrences the further
    into the future they fall — 0 (default) reproduces today's flat-forever
    behavior exactly. `extra_items` are transient HypotheticalItem lines
    (never persisted) scoped to this account, used for "what if I added
    this budget line" scenario forecasting. `one_off_events` are transient
    OneOffEvent charges (e.g. a hypothetical one-time payment) scoped to
    this account.
    """
    events: list[tuple[dt.date, float, str]] = []
    items: list[BudgetItem | HypotheticalItem] = list(
        active_budget_items(session).filter_by(account_id=account.id).all()
    )
    items += [x for x in (extra_items or []) if x.account_id == account.id]
    for item in items:
        if item.flow_type == FlowType.TRANSFER:
            signed = -item.amount
            target_name = item.target_account.name if item.target_account else "?"
            description = f"{item.description} → {target_name}"
        else:
            signed = item.amount if item.flow_type == FlowType.INCOME else -item.amount
            description = item.description
        for occ in generate_occurrences(
            item.effective_from, item.effective_until, item.frequency, compute_start, compute_end
        ):
            multiplier = (
                _income_growth_multiplier(occ, income_growth_rate)
                if item.flow_type == FlowType.INCOME
                else 1.0
            )
            events.append((occ, signed * multiplier, description))

    for one_off in one_off_events or []:
        if one_off.account_id == account.id and compute_start <= one_off.date <= compute_end:
            events.append((one_off.date, one_off.amount, one_off.description))

    for exp in (
        session.query(UpcomingExpense)
        .filter(UpcomingExpense.account_id == account.id)
        .filter(UpcomingExpense.date >= compute_start)
        .filter(UpcomingExpense.date <= compute_end)
    ):
        if exp.flow_type == FlowType.TRANSFER:
            signed = -exp.amount
            target_name = exp.target_account.name if exp.target_account else "?"
            description = f"{exp.description} → {target_name}"
        else:
            signed = exp.amount if exp.flow_type == FlowType.INCOME else -exp.amount
            description = exp.description
        events.append((exp.date, signed, description))

    return events


def _monthly_cc_payment_events(
    account: Account, events: list[tuple[dt.date, float, str]], compute_start: dt.date, compute_end: dt.date
) -> list[tuple[dt.date, float, str]]:
    """(date, +amount, label) autopay credits for a credit-card account with
    cc_payment_day set, posted monthly on that day (clamped to the last day
    of shorter months). Sized to clear the outstanding balance in full
    (cc_pay_in_full) or to a fixed cc_fixed_payment_amount — never negative,
    since a card in credit doesn't need a payment."""
    postings: list[dt.date] = []
    month_cursor = compute_start.replace(day=1)
    while month_cursor <= compute_end:
        last_day = calendar.monthrange(month_cursor.year, month_cursor.month)[1]
        posting = dt.date(month_cursor.year, month_cursor.month, min(account.cc_payment_day, last_day))
        if compute_start <= posting <= compute_end:
            postings.append(posting)
        month_cursor += relativedelta(months=1)
    if not postings:
        return []

    amounts_by_date: dict[dt.date, float] = defaultdict(float)
    for occ, signed, _ in events:
        amounts_by_date[occ] += signed

    posting_set = set(postings)
    payment_events: list[tuple[dt.date, float, str]] = []
    running = account.current_balance
    current = compute_start
    while current <= compute_end:
        running += amounts_by_date.get(current, 0.0)
        if current in posting_set:
            if account.cc_pay_in_full:
                payment = max(-running, 0.0)
                label = "Card payment (paid in full)"
            else:
                payment = max(account.cc_fixed_payment_amount or 0.0, 0.0)
                label = "Card payment (fixed)"
            if payment:
                payment_events.append((current, payment, label))
                running += payment
        current += dt.timedelta(days=1)
    return payment_events


def _credit_card_autopay_events(
    session: Session,
    cc_account: Account,
    compute_end: dt.date,
    income_growth_rate: float = 0.0,
    extra_items: list[HypotheticalItem] | None = None,
    one_off_events: list[OneOffEvent] | None = None,
) -> list[tuple[dt.date, float, str]]:
    """This credit card's autopay credits, computed from its own
    balance_as_of through compute_end — independent of who's asking (the
    card itself, to pay down its balance, or its payee account, to book the
    matching debit), so it always reflects the same schedule either way."""
    if cc_account.cc_payee_account_id is None or cc_account.cc_payment_day is None:
        return []
    compute_start = cc_account.balance_as_of
    if compute_start > compute_end:
        return []
    own_events = _collect_own_charge_events(
        session, cc_account, compute_start, compute_end, income_growth_rate, extra_items, one_off_events
    )
    growth_events = _monthly_growth_events(cc_account, own_events, compute_start, compute_end)
    return _monthly_cc_payment_events(cc_account, own_events + growth_events, compute_start, compute_end)


def _collect_account_events(
    session: Session,
    account: Account,
    compute_start: dt.date,
    compute_end: dt.date,
    income_growth_rate: float = 0.0,
    extra_items: list[HypotheticalItem] | None = None,
    one_off_events: list[OneOffEvent] | None = None,
) -> list[tuple[dt.date, float, str]]:
    """(date, signed_amount, description) for every occurrence of this account's
    budget items, upcoming expenses, cross-account transfers, credit-card
    autopay and growth accrual within [compute_start, compute_end].

    `income_growth_rate`/`extra_items`/`one_off_events`: see
    `_collect_own_charge_events` — threaded through here too so they also
    affect this account's growth postings and (if it's a credit card)
    autopay sizing."""
    events = _collect_own_charge_events(
        session, account, compute_start, compute_end, income_growth_rate, extra_items, one_off_events
    )

    for item in active_budget_items(session).filter_by(target_account_id=account.id).all():
        description = f"{item.description} (from {item.account.name})"
        for occ in generate_occurrences(
            item.effective_from, item.effective_until, item.frequency, compute_start, compute_end
        ):
            events.append((occ, item.amount, description))

    for exp in (
        session.query(UpcomingExpense)
        .filter(UpcomingExpense.target_account_id == account.id)
        .filter(UpcomingExpense.date >= compute_start)
        .filter(UpcomingExpense.date <= compute_end)
    ):
        description = f"{exp.description} (from {exp.account.name})"
        events.append((exp.date, exp.amount, description))

    events += _monthly_growth_events(account, events, compute_start, compute_end)

    if account.cc_payee_account_id is not None and account.cc_payment_day is not None:
        for d, amount, label in _credit_card_autopay_events(
            session, account, compute_end, income_growth_rate, extra_items, one_off_events
        ):
            if compute_start <= d <= compute_end:
                events.append((d, amount, label))

    for cc_account in session.query(Account).filter(Account.cc_payee_account_id == account.id).all():
        for d, amount, label in _credit_card_autopay_events(
            session, cc_account, compute_end, income_growth_rate, extra_items, one_off_events
        ):
            if compute_start <= d <= compute_end:
                events.append((d, -amount, f"{label} → {cc_account.name}"))

    return events


def _details_by_date(events: list[tuple[dt.date, float, str]]) -> dict[dt.date, str]:
    grouped: dict[dt.date, list[str]] = defaultdict(list)
    for occ, signed, description in events:
        sign = "+" if signed > 0 else "-"
        grouped[occ].append(f"{description} ({sign}£{abs(signed):,.2f})")
    return {d: "; ".join(items) for d, items in grouped.items()}


def _historical_account_daily(
    session: Session, account: Account, range_start: dt.date, hist_end: dt.date
) -> pl.DataFrame:
    """Real transaction history for this account (from imported statements),
    for the portion of a requested range that falls before account.balance_as_of
    — the part the forward projection can't show since it only ever starts from
    that snapshot. Reconstructed by walking every real transaction backward
    from the known current_balance, so it's ground truth, not a forecast."""
    if range_start > hist_end:
        return pl.DataFrame(schema=_EMPTY_FORECAST_SCHEMA)

    base_query = session.query(Transaction).join(Statement).filter(Statement.account_id == account.id)
    earliest = base_query.order_by(Transaction.date).first()
    if earliest is None:
        # No statement data ever imported for this account — there's nothing
        # real to reconstruct, so leave this stretch absent rather than
        # fabricating a flat balance from no evidence.
        return pl.DataFrame(schema=_EMPTY_FORECAST_SCHEMA)
    range_start = max(range_start, earliest.date)
    if range_start > hist_end:
        return pl.DataFrame(schema=_EMPTY_FORECAST_SCHEMA)

    transactions = (
        base_query.filter(Transaction.date >= range_start, Transaction.date <= hist_end)
        .order_by(Transaction.date)
        .all()
    )
    later_net = sum(
        t.amount
        for t in base_query.filter(
            Transaction.date > hist_end, Transaction.date < account.balance_as_of
        ).all()
    )
    running = account.current_balance - later_net  # end-of-day balance on hist_end

    amounts_by_date: dict[dt.date, float] = defaultdict(float)
    details_by_date: dict[dt.date, list[str]] = defaultdict(list)
    for t in transactions:
        amounts_by_date[t.date] += t.amount
        sign = "+" if t.amount > 0 else "-"
        details_by_date[t.date].append(f"{t.description} ({sign}£{abs(t.amount):,.2f})")

    dates = pl.date_range(range_start, hist_end, interval="1d", eager=True).to_list()
    balances = [0.0] * len(dates)
    for i in range(len(dates) - 1, -1, -1):
        balances[i] = running
        running -= amounts_by_date.get(dates[i], 0.0)

    rows = [
        {
            "date": d,
            "in": max(amounts_by_date.get(d, 0.0), 0.0),
            "out": max(-amounts_by_date.get(d, 0.0), 0.0),
            "net": amounts_by_date.get(d, 0.0),
            "balance": balances[i],
            "below_threshold": (
                account.low_balance_threshold is not None and balances[i] < account.low_balance_threshold
            ),
            "details": "; ".join(details_by_date.get(d, [])),
        }
        for i, d in enumerate(dates)
    ]
    return pl.DataFrame(rows, schema=_EMPTY_FORECAST_SCHEMA)


def account_daily_forecast(
    session: Session,
    account: Account,
    range_start: dt.date,
    range_end: dt.date,
    income_growth_rate: float = 0.0,
    extra_items: list[HypotheticalItem] | None = None,
    one_off_events: list[OneOffEvent] | None = None,
) -> pl.DataFrame:
    """Daily in/out/net/balance/details for one account, sliced to [range_start, range_end].

    Internally the running balance is computed forward from account.balance_as_of
    (which may fall before range_start) so the balance shown on range_start is
    accurate rather than reset to the opening figure. `details` lists which
    budget items / upcoming expenses contributed to that day's net figure.

    Any portion of the range before balance_as_of is real transaction history
    (from imported statements), not a projection — see _historical_account_daily.

    `income_growth_rate` (annual %, default 0), `extra_items` (transient
    HypotheticalItem lines, default none) and `one_off_events` (transient
    OneOffEvent charges, default none) are scenario-forecasting hooks — see
    app/scenario_sim.py — that leave every existing caller's behavior
    unchanged when omitted.
    """
    hist_df = pl.DataFrame(schema=_EMPTY_FORECAST_SCHEMA)
    if range_start < account.balance_as_of:
        hist_end = min(range_end, account.balance_as_of - dt.timedelta(days=1))
        hist_df = _historical_account_daily(session, account, range_start, hist_end)

    if account.balance_as_of > range_end:
        return hist_df

    compute_start = account.balance_as_of
    compute_end = range_end

    events = _collect_account_events(
        session, account, compute_start, compute_end, income_growth_rate, extra_items, one_off_events
    )
    details_map = _details_by_date(events)

    date_series = pl.date_range(compute_start, compute_end, interval="1d", eager=True)
    base = pl.DataFrame({"date": date_series})

    if events:
        ev_df = pl.DataFrame([(d, a) for d, a, _ in events], schema=["date", "amount"], orient="row")
        in_df = ev_df.filter(pl.col("amount") > 0).group_by("date").agg(pl.col("amount").sum().alias("in"))
        out_df = (
            ev_df.filter(pl.col("amount") < 0).group_by("date").agg((-pl.col("amount")).sum().alias("out"))
        )
    else:
        in_df = pl.DataFrame(schema={"date": pl.Date, "in": pl.Float64})
        out_df = pl.DataFrame(schema={"date": pl.Date, "out": pl.Float64})

    df = (
        base.join(in_df, on="date", how="left")
        .join(out_df, on="date", how="left")
        .with_columns([pl.col("in").fill_null(0.0), pl.col("out").fill_null(0.0)])
        .with_columns((pl.col("in") - pl.col("out")).alias("net"))
        .sort("date")
    )

    first_net = df["net"][0]
    df = (
        df.with_columns(pl.col("net").cum_sum().alias("_cum"))
        .with_columns((pl.lit(account.current_balance) + pl.col("_cum") - pl.lit(first_net)).alias("balance"))
        .drop("_cum")
    )

    if account.low_balance_threshold is not None:
        df = df.with_columns((pl.col("balance") < account.low_balance_threshold).alias("below_threshold"))
    else:
        df = df.with_columns(pl.lit(False).alias("below_threshold"))

    details_col = [details_map.get(d, "") for d in date_series.to_list()]
    df = df.with_columns(pl.Series("details", details_col))

    forward_df = df.filter((pl.col("date") >= range_start) & (pl.col("date") <= range_end))
    return pl.concat([hist_df, forward_df]) if hist_df.height else forward_df


def combined_daily_forecast(
    session: Session,
    range_start: dt.date,
    range_end: dt.date,
    accounts: list[Account] | None = None,
    income_growth_rate: float = 0.0,
    extra_items: list[HypotheticalItem] | None = None,
    one_off_events: list[OneOffEvent] | None = None,
) -> pl.DataFrame:
    """Combined in/out/net/balance across `accounts` (default: every account),
    valid from the latest balance_as_of onward, plus a per-account balance
    breakdown and a details column listing every contributing item that day
    (prefixed by account).

    `income_growth_rate`/`extra_items`/`one_off_events`: see
    `account_daily_forecast` — opt-in scenario-forecasting hooks, default
    behavior unchanged when omitted."""
    accounts = accounts if accounts is not None else session.query(Account).all()
    empty_schema = {
        "date": pl.Date,
        "in": pl.Float64,
        "out": pl.Float64,
        "net": pl.Float64,
        "balance": pl.Float64,
        "details": pl.Utf8,
    }
    if not accounts:
        return pl.DataFrame(schema=empty_schema)

    combined_start = max(a.balance_as_of for a in accounts)
    effective_start = max(combined_start, range_start)
    if effective_start > range_end:
        return pl.DataFrame(schema=empty_schema)

    per_account = []
    all_details: dict[dt.date, list[str]] = defaultdict(list)
    for account in accounts:
        df = account_daily_forecast(
            session, account, effective_start, range_end, income_growth_rate, extra_items, one_off_events
        )
        if df.height == 0:
            continue
        per_account.append(
            df.select(["date", "in", "out", "net", "balance"]).rename(
                {
                    "in": f"{account.name} in",
                    "out": f"{account.name} out",
                    "net": f"{account.name} net",
                    "balance": account.name,
                }
            )
        )
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

    merged = merged.with_columns(
        [
            pl.sum_horizontal(balance_cols).alias("balance"),
            pl.sum_horizontal(in_cols).alias("in"),
            pl.sum_horizontal(out_cols).alias("out"),
        ]
    )
    merged = merged.with_columns((pl.col("in") - pl.col("out")).alias("net"))

    details_col = ["; ".join(all_details.get(d, [])) for d in merged["date"].to_list()]
    merged = merged.with_columns(pl.Series("details", details_col))

    net_cols = [f"{a.name} net" for a in accounts if f"{a.name} net" in merged.columns]
    return merged.select(
        [
            "date",
            "in",
            "out",
            "net",
            "balance",
            "details",
            *balance_cols,
            *[c for c in merged.columns if c in in_cols + out_cols + net_cols],
        ]
    )


def low_balance_warnings(
    session: Session, range_start: dt.date, range_end: dt.date, accounts: list[Account] | None = None
) -> list[dict]:
    """Every (account, date) in the range where the forecast balance dips below
    threshold, restricted to `accounts` (default: every account)."""
    warnings = []
    for account in accounts if accounts is not None else session.query(Account).all():
        if account.low_balance_threshold is None:
            continue
        df = account_daily_forecast(session, account, range_start, range_end)
        for row in df.filter(pl.col("below_threshold")).iter_rows(named=True):
            warnings.append(
                {
                    "account": account.name,
                    "date": row["date"],
                    "balance": row["balance"],
                    "threshold": account.low_balance_threshold,
                }
            )
    return warnings


def account_run_rate(session: Session, account: Account, horizon_days: int = 365) -> dict:
    """Average monthly net (in - out) and the first date (if any) this
    account's forecast balance goes negative, projected from balance_as_of
    over the next `horizon_days` using its current budget items/upcoming
    expenses/transfers/growth — used to flag an account that's being drawn
    down faster than it's topped up, or heading for an overdraft."""
    start = account.balance_as_of
    end = start + dt.timedelta(days=horizon_days)
    df = account_daily_forecast(session, account, start, end)
    if df.height == 0:
        return {"avg_monthly_net": 0.0, "overdrawn_date": None}
    total_net = df["net"].sum()
    months = df.height / 30.44
    overdrawn = df.filter(pl.col("balance") < 0)
    return {
        "avg_monthly_net": total_net / months if months else 0.0,
        "overdrawn_date": overdrawn["date"][0] if overdrawn.height else None,
    }


def bucket_date_ranges(dates: list[dt.date], frequency: str) -> list[tuple[int, int]]:
    """Row-index (start, end) inclusive spans grouping `dates` (assumed sorted
    ascending, one entry per row) into Daily/Weekly/Monthly buckets — shared
    by any front end that charts a daily forecast at a coarser granularity."""
    if not dates:
        return []
    if frequency == "Daily":
        return [(i, i) for i in range(len(dates))]

    def key(d: dt.date):
        if frequency == "Weekly":
            iso = d.isocalendar()
            return (iso[0], iso[1])
        return (d.year, d.month)

    ranges: list[tuple[int, int]] = []
    start = 0
    current_key = key(dates[0])
    for i in range(1, len(dates)):
        k = key(dates[i])
        if k != current_key:
            ranges.append((start, i - 1))
            start = i
            current_key = k
    ranges.append((start, len(dates) - 1))
    return ranges
