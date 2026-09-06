import calendar
import datetime as dt

from mcp.server.mcpserver import MCPServer

from app import investment_sim, market_data
from app.db import get_session, init_db
from app.forecast import (
    account_daily_forecast,
    combined_daily_forecast,
    low_balance_warnings,
    monthly_budget_summary,
)
from app.models import (
    Account,
    BudgetItem,
    BudgetSuggestion,
    Category,
    FlowType,
    Frequency,
    Holding,
    Statement,
    SuggestionStatus,
    SuggestionType,
    Transaction,
    UpcomingExpense,
    UpcomingExpenseStatus,
)
from app.seed import get_or_create_category, seed_defaults
from app.statement_import import (
    UNCATEGORIZED,
    accept_suggestion as _accept_suggestion,
    budget_vs_actual_report as _budget_vs_actual_report,
    import_statement as _import_statement_core,
    reject_suggestion as _reject_suggestion,
)
from app.statements import (
    extract_csv_transactions,
    find_recurring_transactions as _find_recurring,
    read_pdf_text as _read_pdf_text,
)

server = MCPServer(
    "household-budgeting",
    instructions=(
        "Tools for the household budgeting app: analysing bank/card statements, "
        "importing them to save classified transactions, comparing actuals against the "
        "budget, and reading/writing the same SQLite data the desktop GUI uses (accounts, "
        "recurring budget items, one-off upcoming expenses, budget suggestions, and "
        "cashflow forecasts). Money amounts on committed budget items and upcoming "
        "expenses are always positive; direction (income vs expense) is a separate field. "
        "Imported transactions and statements keep their own signed `amount` (+ in / - out). "
        "Any account can hold investment tickers+weights (set_holdings) — this drives both "
        "the deterministic cashflow forecast and run_investment_simulation's historical-"
        "bootstrap Monte Carlo."
    ),
)

init_db()
_seed_session = get_session()
seed_defaults(_seed_session)
_seed_session.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _account_to_dict(a: Account) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "current_balance": a.current_balance,
        "balance_as_of": a.balance_as_of.isoformat(),
        "low_balance_threshold": a.low_balance_threshold,
        "growth_rate": a.growth_rate,
        "holdings": {h.ticker: h.weight for h in a.holdings},
        "is_credit_card": a.is_credit_card,
        "cc_payee_account": a.cc_payee_account.name if a.cc_payee_account else None,
        "cc_payment_day": a.cc_payment_day,
        "cc_pay_in_full": a.cc_pay_in_full,
        "cc_fixed_payment_amount": a.cc_fixed_payment_amount,
    }


def _budget_item_to_dict(b: BudgetItem) -> dict:
    return {
        "id": b.id,
        "description": b.description,
        "amount": b.amount,
        "flow_type": b.flow_type.value,
        "frequency": b.frequency.value,
        "effective_from": b.effective_from.isoformat(),
        "effective_until": b.effective_until.isoformat() if b.effective_until else None,
        "category": b.category.name,
        "account": b.account.name,
        "target_account": b.target_account.name if b.target_account else None,
        "notes": b.notes,
    }


def _upcoming_to_dict(u: UpcomingExpense) -> dict:
    return {
        "id": u.id,
        "date": u.date.isoformat(),
        "description": u.description,
        "amount": u.amount,
        "flow_type": u.flow_type.value,
        "category": u.category.name,
        "account": u.account.name,
        "target_account": u.target_account.name if u.target_account else None,
        "status": u.status.value,
        "matched_transaction_id": u.matched_transaction_id,
        "last_seen_statement_id": u.last_seen_statement_id,
    }


def _statement_to_dict(s: Statement) -> dict:
    return {
        "id": s.id,
        "account": s.account.name,
        "period_start": s.period_start.isoformat(),
        "period_end": s.period_end.isoformat(),
        "imported_at": s.imported_at.isoformat(),
        "source_note": s.source_note,
        "transaction_count": len(s.transactions),
        "net": round(sum(t.amount for t in s.transactions), 2),
    }


def _transaction_to_dict(t: Transaction) -> dict:
    return {
        "id": t.id,
        "statement_id": t.statement_id,
        "date": t.date.isoformat(),
        "description": t.description,
        "amount": t.amount,
        "category": t.category.name if t.category else None,
        "matched_budget_item_id": t.matched_budget_item_id,
        "matched_budget_item": t.matched_budget_item.description if t.matched_budget_item else None,
    }


def _suggestion_to_dict(sg: BudgetSuggestion) -> dict:
    return {
        "id": sg.id,
        "type": sg.suggestion_type.value,
        "status": sg.status.value,
        "account": sg.account.name,
        "category": sg.category.name,
        "description": sg.description,
        "proposed_amount": sg.proposed_amount,
        "proposed_frequency": sg.proposed_frequency.value,
        "budget_item_id": sg.budget_item_id,
        "current_amount": sg.current_amount,
        "rationale": sg.rationale,
        "created_at": sg.created_at.isoformat(),
        "decided_at": sg.decided_at.isoformat() if sg.decided_at else None,
    }


def _resolve_suggestion_status(value: str) -> SuggestionStatus:
    for s in SuggestionStatus:
        if s.value.lower() == value.lower() or s.name.lower() == value.lower():
            return s
    raise ValueError(
        f"Unknown status '{value}'. Valid values: {[s.value for s in SuggestionStatus]}, or 'All'."
    )


def _resolve_account(session, name: str) -> Account:
    account = session.query(Account).filter(Account.name == name).one_or_none()
    if account is None:
        names = [a.name for a in session.query(Account).order_by(Account.name).all()]
        raise ValueError(f"No account named '{name}'. Existing accounts: {names}. Use create_account first.")
    return account


def _resolve_frequency(value: str) -> Frequency:
    for f in Frequency:
        if f.value.lower() == value.lower() or f.name.lower() == value.lower():
            return f
    raise ValueError(f"Unknown frequency '{value}'. Valid values: {[f.value for f in Frequency]}")


def _resolve_flow_type(value: str) -> FlowType:
    for ft in FlowType:
        if ft.value.lower() == value.lower() or ft.name.lower() == value.lower():
            return ft
    raise ValueError(f"Unknown type '{value}'. Valid values: {[ft.value for ft in FlowType]}")


def _resolve_upcoming_status(value: str) -> UpcomingExpenseStatus:
    for s in UpcomingExpenseStatus:
        if s.value.lower() == value.lower() or s.name.lower() == value.lower():
            return s
    raise ValueError(f"Unknown status '{value}'. Valid values: {[s.value for s in UpcomingExpenseStatus]}")


# ---------------------------------------------------------------------------
# statement analysis
# ---------------------------------------------------------------------------


@server.tool()
def read_pdf_statement(file_path: str, first_page: int = 1, last_page: int | None = None) -> str:
    """Extract raw text from a PDF bank/card statement, page by page.

    Use this for statements that aren't a clean CSV (e.g. HSBC, Amex). Bank
    PDF layouts vary too much for one hardcoded table parser, so this hands
    back readable text for you to interpret directly — read it, transcribe
    the transactions you find (date, description, signed amount) into a
    list, then optionally pass that list to find_recurring_transactions.
    For long statements, page through with first_page/last_page.
    """
    return _read_pdf_text(file_path, first_page, last_page)


@server.tool()
def extract_csv_statement(file_path: str) -> list[dict]:
    """Parse a bank/card CSV export into transactions.

    Returns a list of {date, description, amount, category_hint,
    source_format}, where amount is signed (positive = money in, negative =
    money out). Recognises the Monzo export format specifically; otherwise
    sniffs Date/Description/Amount-style columns, which covers most UK bank
    CSV exports.
    """
    return extract_csv_transactions(file_path)


@server.tool()
def find_recurring_transactions(
    transactions: list[dict], min_occurrences: int = 2, amount_tolerance_pct: float = 0.15
) -> list[dict]:
    """Group transactions by normalised merchant name and flag recurring-bill candidates.

    Each transaction dict needs `date` (ISO string), `description`, and
    `amount` (signed). Feed it the output of extract_csv_statement, or a
    list you assembled yourself after reading a PDF with read_pdf_statement.
    Returns, per merchant: occurrence count, first/last date, avg/min/max
    amount, the most common day of month, and a guessed frequency — the
    same kind of analysis used to cross-check this budget's recurring bills
    in the first place.
    """
    return _find_recurring(transactions, min_occurrences, amount_tolerance_pct)


# ---------------------------------------------------------------------------
# statement import / budget suggestions
# ---------------------------------------------------------------------------


@server.tool()
def import_statement(
    account: str,
    transactions: list[dict],
    period_start: str | None = None,
    period_end: str | None = None,
    source_note: str | None = None,
) -> dict:
    """Save a billing period's transactions, classify them, update the account
    balance, and refresh this account's budget suggestions.

    `transactions` is a list of {date, description, amount} — the direct
    output of extract_csv_statement, or a list you assemble by hand after
    reading a PDF with read_pdf_statement (same handoff used by
    find_recurring_transactions). Amount is signed: positive = money in,
    negative = money out.

    period_start/period_end (ISO dates) default to the min/max transaction
    date if omitted. Statements must be imported in chronological order per
    account: the balance is updated by summing the imported transactions
    onto current_balance and advancing balance_as_of to period_end, so an
    out-of-order (backdated) import is rejected to avoid double-counting.

    Returns the saved statement, its budget-vs-actual report for the period,
    and this account's currently pending budget suggestions (new ones from
    this import, plus any still outstanding from before).
    """
    session = get_session()
    try:
        acc = _resolve_account(session, account)
        if not transactions:
            raise ValueError("No transactions to import.")
        dates = [
            t["date"] if isinstance(t["date"], dt.date) else dt.date.fromisoformat(t["date"])
            for t in transactions
        ]
        start = _parse_date(period_start) or min(dates)
        end = _parse_date(period_end) or max(dates)

        statement = _import_statement_core(session, acc, transactions, start, end, source_note)
        report = _budget_vs_actual_report(session, statement)
        suggestions = (
            session.query(BudgetSuggestion)
            .filter_by(account_id=acc.id, status=SuggestionStatus.PENDING)
            .order_by(BudgetSuggestion.created_at.desc())
            .all()
        )
        return {
            "statement": _statement_to_dict(statement),
            "budget_vs_actual": report,
            "pending_suggestions": [_suggestion_to_dict(s) for s in suggestions],
        }
    finally:
        session.close()


@server.tool()
def list_statements(account: str | None = None) -> list[dict]:
    """List imported statements, optionally filtered to one account."""
    session = get_session()
    try:
        query = session.query(Statement)
        if account:
            acc = _resolve_account(session, account)
            query = query.filter(Statement.account_id == acc.id)
        return [_statement_to_dict(s) for s in query.order_by(Statement.period_start).all()]
    finally:
        session.close()


@server.tool()
def get_statement_report(statement_id: int) -> dict:
    """Budget-vs-actual report for an already-imported statement."""
    session = get_session()
    try:
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise ValueError(f"No statement with id {statement_id}")
        return _budget_vs_actual_report(session, statement)
    finally:
        session.close()


@server.tool()
def list_transactions(
    statement_id: int | None = None,
    account: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    uncategorized_only: bool = False,
) -> list[dict]:
    """List saved transactions, filtered by any combination of statement,
    account, date range, or category. Set uncategorized_only=True to find
    transactions the keyword classifier couldn't place, for manual review
    (see update_transaction_category)."""
    session = get_session()
    try:
        query = session.query(Transaction)
        if statement_id is not None:
            query = query.filter(Transaction.statement_id == statement_id)
        if account:
            acc = _resolve_account(session, account)
            query = query.join(Statement).filter(Statement.account_id == acc.id)
        if start_date:
            query = query.filter(Transaction.date >= _parse_date(start_date))
        if end_date:
            query = query.filter(Transaction.date <= _parse_date(end_date))
        if category:
            query = query.filter(Transaction.category.has(Category.name == category))
        if uncategorized_only:
            query = query.filter(
                (Transaction.category_id.is_(None)) | Transaction.category.has(Category.name == UNCATEGORIZED)
            )
        return [_transaction_to_dict(t) for t in query.order_by(Transaction.date).all()]
    finally:
        session.close()


@server.tool()
def update_transaction_category(transaction_id: int, category: str) -> dict:
    """Manually reclassify a transaction (the keyword classifier is best-effort)."""
    session = get_session()
    try:
        t = session.get(Transaction, transaction_id)
        if t is None:
            raise ValueError(f"No transaction with id {transaction_id}")
        t.category = get_or_create_category(session, category)
        session.commit()
        return _transaction_to_dict(t)
    finally:
        session.close()


@server.tool()
def delete_statement(statement_id: int) -> dict:
    """Delete a statement and its transactions, and best-effort reverse its
    effect on the account balance (subtracts the statement's net back out of
    current_balance). Doesn't rewind balance_as_of if a later statement has
    since been imported for this account — use update_account to correct
    the balance manually in that case."""
    session = get_session()
    try:
        statement = session.get(Statement, statement_id)
        if statement is None:
            raise ValueError(f"No statement with id {statement_id}")
        account = statement.account
        net = sum(t.amount for t in statement.transactions)
        account.current_balance -= net
        period = f"{statement.period_start.isoformat()} to {statement.period_end.isoformat()}"
        session.delete(statement)
        session.commit()
        return {"deleted": f"{account.name} statement ({period})", "balance_adjustment": round(-net, 2)}
    finally:
        session.close()


@server.tool()
def list_budget_suggestions(status: str = "Pending", account: str | None = None) -> list[dict]:
    """List budget suggestions generated from statement imports.

    status: 'Pending' (default), 'Accepted', 'Rejected', or 'All'.
    """
    session = get_session()
    try:
        query = session.query(BudgetSuggestion)
        if status.lower() != "all":
            query = query.filter(BudgetSuggestion.status == _resolve_suggestion_status(status))
        if account:
            acc = _resolve_account(session, account)
            query = query.filter(BudgetSuggestion.account_id == acc.id)
        return [_suggestion_to_dict(s) for s in query.order_by(BudgetSuggestion.created_at.desc()).all()]
    finally:
        session.close()


@server.tool()
def accept_budget_suggestion(suggestion_id: int) -> dict:
    """Accept a pending budget suggestion: creates a new Budget Item (for a
    'New Item' suggestion) or updates the existing item's amount (for an
    'Amount Change' suggestion). The new/updated item's effective_from is
    today, so it doesn't retroactively change past budget summaries."""
    session = get_session()
    try:
        sg = session.get(BudgetSuggestion, suggestion_id)
        if sg is None:
            raise ValueError(f"No budget suggestion with id {suggestion_id}")
        item = _accept_suggestion(session, sg)
        return {"suggestion": _suggestion_to_dict(sg), "budget_item": _budget_item_to_dict(item)}
    finally:
        session.close()


@server.tool()
def reject_budget_suggestion(suggestion_id: int) -> dict:
    """Reject a pending budget suggestion. Rejected suggestions aren't
    re-proposed by future imports."""
    session = get_session()
    try:
        sg = session.get(BudgetSuggestion, suggestion_id)
        if sg is None:
            raise ValueError(f"No budget suggestion with id {suggestion_id}")
        _reject_suggestion(session, sg)
        return _suggestion_to_dict(sg)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


@server.tool()
def list_accounts() -> list[dict]:
    """List all accounts with their current balance, balance-as-of date, and low-balance threshold."""
    session = get_session()
    try:
        return [_account_to_dict(a) for a in session.query(Account).order_by(Account.name).all()]
    finally:
        session.close()


@server.tool()
def create_account(
    name: str,
    current_balance: float = 0.0,
    balance_as_of: str | None = None,
    low_balance_threshold: float | None = None,
    growth_rate: float | None = None,
    is_credit_card: bool = False,
    cc_payee_account: str | None = None,
    cc_payment_day: int | None = None,
    cc_pay_in_full: bool = False,
    cc_fixed_payment_amount: float | None = None,
) -> dict:
    """Create a new account. balance_as_of is an ISO date string (e.g. '2026-08-02'), defaults to today.

    growth_rate is an annual percentage (e.g. 4.5 for 4.5% APY), compounded
    monthly in the cashflow forecast for this account.

    is_credit_card=True enables autopay projection: on cc_payment_day each
    month (1-31, clamped to shorter months), a direct debit is projected
    from cc_payee_account (must already exist) into this account — either
    the full outstanding balance (cc_pay_in_full=True) or a fixed
    cc_fixed_payment_amount. current_balance follows the usual sign
    convention (negative = money owed).
    """
    session = get_session()
    try:
        if session.query(Account).filter_by(name=name).one_or_none() is not None:
            raise ValueError(f"An account named '{name}' already exists.")
        if is_credit_card and not cc_payee_account:
            raise ValueError("is_credit_card=True requires cc_payee_account.")
        account = Account(
            name=name,
            current_balance=current_balance,
            balance_as_of=_parse_date(balance_as_of) or dt.date.today(),
            low_balance_threshold=low_balance_threshold,
            growth_rate=growth_rate,
            is_credit_card=is_credit_card,
            cc_payee_account=_resolve_account(session, cc_payee_account) if cc_payee_account else None,
            cc_payment_day=cc_payment_day if is_credit_card else None,
            cc_pay_in_full=cc_pay_in_full if is_credit_card else False,
            cc_fixed_payment_amount=cc_fixed_payment_amount
            if is_credit_card and not cc_pay_in_full
            else None,
        )
        session.add(account)
        session.commit()
        return _account_to_dict(account)
    finally:
        session.close()


@server.tool()
def update_account(
    account_id: int,
    name: str | None = None,
    current_balance: float | None = None,
    balance_as_of: str | None = None,
    low_balance_threshold: float | None = None,
    clear_threshold: bool = False,
    growth_rate: float | None = None,
    clear_growth_rate: bool = False,
    is_credit_card: bool | None = None,
    cc_payee_account: str | None = None,
    clear_cc_payee_account: bool = False,
    cc_payment_day: int | None = None,
    cc_pay_in_full: bool | None = None,
    cc_fixed_payment_amount: float | None = None,
    clear_cc_fixed_payment_amount: bool = False,
) -> dict:
    """Update an account. Only pass the fields you want to change; set clear_threshold=True to remove a
    warning threshold, clear_growth_rate=True to remove a growth rate.

    Credit card autopay fields (cc_payee_account, cc_payment_day,
    cc_pay_in_full, cc_fixed_payment_amount) only take effect once
    is_credit_card is (or was already) True.
    """
    session = get_session()
    try:
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError(f"No account with id {account_id}")
        if name is not None:
            account.name = name
        if current_balance is not None:
            account.current_balance = current_balance
        if balance_as_of is not None:
            account.balance_as_of = _parse_date(balance_as_of)
        if clear_threshold:
            account.low_balance_threshold = None
        elif low_balance_threshold is not None:
            account.low_balance_threshold = low_balance_threshold
        if clear_growth_rate:
            account.growth_rate = None
        elif growth_rate is not None:
            account.growth_rate = growth_rate
        if is_credit_card is not None:
            account.is_credit_card = is_credit_card
        if clear_cc_payee_account:
            account.cc_payee_account = None
        elif cc_payee_account is not None:
            account.cc_payee_account = _resolve_account(session, cc_payee_account)
        if cc_payment_day is not None:
            account.cc_payment_day = cc_payment_day
        if cc_pay_in_full is not None:
            account.cc_pay_in_full = cc_pay_in_full
        if clear_cc_fixed_payment_amount:
            account.cc_fixed_payment_amount = None
        elif cc_fixed_payment_amount is not None:
            account.cc_fixed_payment_amount = cc_fixed_payment_amount
        if account.is_credit_card and account.cc_payee_account_id == account.id:
            raise ValueError("A credit card can't pay itself — choose a different cc_payee_account.")
        session.commit()
        return _account_to_dict(account)
    finally:
        session.close()


@server.tool()
def delete_account(account_id: int) -> dict:
    """Delete an account. Refused if any budget item or upcoming expense still references it."""
    session = get_session()
    try:
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError(f"No account with id {account_id}")
        used_budget = session.query(BudgetItem).filter_by(account_id=account_id).count()
        used_upcoming = session.query(UpcomingExpense).filter_by(account_id=account_id).count()
        used_as_target = (
            session.query(BudgetItem).filter_by(target_account_id=account_id).count()
            + session.query(UpcomingExpense).filter_by(target_account_id=account_id).count()
        )
        used_as_cc_payee = session.query(Account).filter_by(cc_payee_account_id=account_id).count()
        if used_budget or used_upcoming or used_as_target or used_as_cc_payee:
            raise ValueError(
                f"Can't delete '{account.name}' — used by {used_budget} budget item(s), "
                f"{used_upcoming} upcoming expense(s), {used_as_target} transfer(s) targeting it, and "
                f"{used_as_cc_payee} credit card(s) that pay from it. Reassign or delete those first."
            )
        name = account.name
        session.delete(account)
        session.commit()
        return {"deleted": name}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# investment accounts: holdings + historical-bootstrap Monte Carlo simulation
# ---------------------------------------------------------------------------


@server.tool()
def list_holdings(account: str) -> dict[str, float]:
    """{ticker: weight} for an account's investment holdings (empty dict if it's not an
    investment account)."""
    session = get_session()
    try:
        acc = _resolve_account(session, account)
        return {h.ticker: h.weight for h in acc.holdings}
    finally:
        session.close()


@server.tool()
def set_holdings(account: str, weights: dict[str, float]) -> dict:
    """Replace an account's investment holdings with `weights` ({ticker: relative weight} —
    needn't sum to 1, they're renormalised). Pass an empty dict to clear holdings (making it a
    plain account again). Having any holdings is what makes an account an "investment account":
    it drives both the deterministic cashflow forecast's growth (mean historical monthly return
    of the portfolio) and run_investment_simulation.
    """
    session = get_session()
    try:
        acc = _resolve_account(session, account)
        acc.holdings.clear()  # cascade="all, delete-orphan" — also updates the in-memory collection
        for ticker, weight in weights.items():
            session.add(Holding(account=acc, ticker=ticker.upper(), weight=weight))
        session.commit()
        return _account_to_dict(acc)
    finally:
        session.close()


@server.tool()
def run_investment_simulation(
    account: str,
    as_of: str | None = None,
    lookback_years: int = 10,
    horizon_years: int = 20,
    monthly_contribution: float = 0.0,
    n_paths: int = 1000,
    return_shift_grid: list[float] | None = None,
    vol_scale_grid: list[float] | None = None,
    contribution_grid: list[float] | None = None,
    horizon_grid_years: list[int] | None = None,
    seed: int | None = None,
) -> dict:
    """Historical-bootstrap Monte Carlo simulation for an investment account (one with holdings
    set via set_holdings). Resamples the portfolio's actual historical monthly returns (fetched
    from Yahoo Finance) rather than assuming a parametric distribution.

    Always returns, for the base case (no return/vol adjustment):
      - percentile_bands: 5/25/50/75/95th percentile ending-value trajectories, monthly, over horizon_years
      - drawdown_stats: distribution of each simulated path's own worst peak-to-trough decline
        (worst/best/mean and 5/25/50/75/95th percentiles, as negative fractions) — how bad the ride
        could get, separate from where you end up. Computed on the raw balance, so a steady
        monthly_contribution partially masks the underlying market decline.

    Optionally also runs one or both parameter grids (each cell reports median_ending_balance and
    median_max_drawdown at horizon_years):
      - return_shift_grid × vol_scale_grid: "what if returns/volatility were different from
        history?" (e.g. return_shift_grid=[-0.02,0,0.02], vol_scale_grid=[0.5,1.0,1.5])
      - contribution_grid × horizon_grid_years: "how much do I need to save, for how long?"
    """
    session = get_session()
    try:
        acc = _resolve_account(session, account)
        weights = {h.ticker: h.weight for h in acc.holdings}
        if not weights:
            raise ValueError(f"'{acc.name}' has no holdings. Set some first with set_holdings.")
        as_of_date = _parse_date(as_of) or dt.date.today()
        returns = market_data.fetch_portfolio_monthly_returns(weights, as_of_date, lookback_years)
        if not returns:
            raise ValueError(
                "Couldn't fetch historical price data for this portfolio (bad ticker or no network)."
            )

        n_periods = horizon_years * 12
        paths = investment_sim.bootstrap_paths(
            returns,
            acc.current_balance,
            n_periods,
            n_paths=n_paths,
            monthly_contribution=monthly_contribution,
            seed=seed,
        )
        result = {
            "account": acc.name,
            "as_of": as_of_date.isoformat(),
            "lookback_years": lookback_years,
            "horizon_years": horizon_years,
            "historical_monthly_returns_used": len(returns),
            "mean_historical_monthly_return": sum(returns) / len(returns),
            "percentile_bands": investment_sim.percentile_bands(paths),
            "drawdown_stats": investment_sim.drawdown_stats(paths),
        }

        if return_shift_grid and vol_scale_grid:
            grid = investment_sim.return_vol_grid(
                returns,
                acc.current_balance,
                n_periods,
                return_shift_grid,
                vol_scale_grid,
                monthly_contribution=monthly_contribution,
                seed=seed,
            )
            result["return_vol_grid"] = [
                {"return_shift": rs, "vol_scale": vs, **stats} for (rs, vs), stats in grid.items()
            ]

        if contribution_grid and horizon_grid_years:
            grid = investment_sim.contribution_horizon_grid(
                returns, acc.current_balance, contribution_grid, horizon_grid_years, seed=seed
            )
            result["contribution_horizon_grid"] = [
                {"monthly_contribution": c, "horizon_years": y, **stats} for (c, y), stats in grid.items()
            ]

        return result
    finally:
        session.close()


@server.tool()
def run_realised_historical_scenarios(
    account: str,
    as_of: str | None = None,
    lookback_years: int = 20,
    horizon_years: int = 10,
    monthly_contribution: float = 0.0,
    include_trajectories: bool = False,
) -> dict:
    """Every real, non-random horizon_years-long historical window for an investment account's
    portfolio (one with holdings set via set_holdings), replayed in actual chronological order —
    "if you'd started investing on date X, here's what would really have happened" — for every X
    the available history allows. Unlike run_investment_simulation, nothing is resampled or
    shuffled: each scenario is a real sequence of returns that occurred.

    lookback_years controls how much history is fetched (and therefore how many overlapping
    horizon_years-long windows/scenarios can be formed — needs at least horizon_years of data,
    more gives more scenarios). Set include_trajectories=True to get each scenario's full
    month-by-month balance path, not just its ending balance and max_drawdown (each scenario's own
    worst peak-to-trough decline, as a negative fraction — also summarised across all scenarios via
    worst/median/best_max_drawdown, alongside the equivalent ending-balance stats).
    """
    session = get_session()
    try:
        acc = _resolve_account(session, account)
        weights = {h.ticker: h.weight for h in acc.holdings}
        if not weights:
            raise ValueError(f"'{acc.name}' has no holdings. Set some first with set_holdings.")
        as_of_date = _parse_date(as_of) or dt.date.today()
        series = market_data.fetch_portfolio_monthly_return_series(weights, as_of_date, lookback_years)
        n_periods = horizon_years * 12
        if len(series) < n_periods:
            raise ValueError(
                f"Only {len(series)} months of history available, need at least {n_periods} "
                f"({horizon_years} years) — increase lookback_years or reduce horizon_years."
            )

        scenarios = investment_sim.realised_historical_scenarios(
            series, acc.current_balance, n_periods, monthly_contribution
        )
        endings = sorted(s["ending_balance"] for s in scenarios)
        drawdowns = sorted(s["max_drawdown"] for s in scenarios)
        if not include_trajectories:
            for s in scenarios:
                del s["trajectory"]

        return {
            "account": acc.name,
            "as_of": as_of_date.isoformat(),
            "horizon_years": horizon_years,
            "scenario_count": len(scenarios),
            "worst_ending_balance": endings[0],
            "median_ending_balance": endings[len(endings) // 2],
            "best_ending_balance": endings[-1],
            "worst_max_drawdown": drawdowns[0],
            "median_max_drawdown": drawdowns[len(drawdowns) // 2],
            "best_max_drawdown": drawdowns[-1],
            "scenarios": scenarios,
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------


@server.tool()
def list_categories() -> list[str]:
    """List all category names used to tag budget items and upcoming expenses."""
    session = get_session()
    try:
        return [c.name for c in session.query(Category).order_by(Category.name).all()]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# budget items
# ---------------------------------------------------------------------------


@server.tool()
def list_budget_items(active_only: bool = False, as_of: str | None = None) -> list[dict]:
    """List committed recurring payments (income or expense).

    If active_only is True, only include items whose effective_from/
    effective_until window covers as_of (defaults to today).
    """
    session = get_session()
    try:
        items = session.query(BudgetItem).order_by(BudgetItem.effective_from).all()
        if active_only:
            ref = _parse_date(as_of) or dt.date.today()
            items = [i for i in items if i.is_active_on(ref)]
        return [_budget_item_to_dict(i) for i in items]
    finally:
        session.close()


@server.tool()
def create_budget_item(
    description: str,
    amount: float,
    category: str,
    account: str,
    flow_type: str = "Expense",
    frequency: str = "Monthly",
    effective_from: str | None = None,
    effective_until: str | None = None,
    notes: str | None = None,
    target_account: str | None = None,
) -> dict:
    """Add a committed recurring payment.

    flow_type: 'Income', 'Expense', or 'Transfer'. frequency: Weekly,
    Fortnightly, 4-Weekly, Monthly, Quarterly, 6-Monthly, or Annually.
    effective_from (ISO date, defaults to today) anchors the recurrence —
    e.g. for Monthly its day-of-month is the day the payment recurs on.
    Category is created automatically if it doesn't exist; account must
    already exist (see list_accounts / create_account).

    flow_type='Transfer' requires target_account (any other existing
    account) — the amount is projected as an outflow from `account` and an
    inflow to `target_account` on the same schedule. Use this for any
    recurring cross-account movement (e.g. into a savings account, which can
    separately be given its own growth_rate via create_account/update_account).
    """
    session = get_session()
    try:
        acc = _resolve_account(session, account)
        resolved_flow_type = _resolve_flow_type(flow_type)
        if resolved_flow_type == FlowType.TRANSFER and not target_account:
            raise ValueError("flow_type='Transfer' requires target_account.")
        item = BudgetItem(
            description=description,
            amount=amount,
            flow_type=resolved_flow_type,
            frequency=_resolve_frequency(frequency),
            effective_from=_parse_date(effective_from) or dt.date.today(),
            effective_until=_parse_date(effective_until),
            notes=notes,
            category=get_or_create_category(session, category),
            account=acc,
            target_account=_resolve_account(session, target_account) if target_account else None,
        )
        session.add(item)
        session.commit()
        return _budget_item_to_dict(item)
    finally:
        session.close()


@server.tool()
def update_budget_item(
    item_id: int,
    description: str | None = None,
    amount: float | None = None,
    category: str | None = None,
    account: str | None = None,
    flow_type: str | None = None,
    frequency: str | None = None,
    effective_from: str | None = None,
    effective_until: str | None = None,
    clear_effective_until: bool = False,
    notes: str | None = None,
    target_account: str | None = None,
    clear_target_account: bool = False,
) -> dict:
    """Update a budget item. Only pass the fields you want to change."""
    session = get_session()
    try:
        item = session.get(BudgetItem, item_id)
        if item is None:
            raise ValueError(f"No budget item with id {item_id}")
        if description is not None:
            item.description = description
        if amount is not None:
            item.amount = amount
        if category is not None:
            item.category = get_or_create_category(session, category)
        if account is not None:
            item.account = _resolve_account(session, account)
        if flow_type is not None:
            item.flow_type = _resolve_flow_type(flow_type)
        if frequency is not None:
            item.frequency = _resolve_frequency(frequency)
        if effective_from is not None:
            item.effective_from = _parse_date(effective_from)
        if clear_effective_until:
            item.effective_until = None
        elif effective_until is not None:
            item.effective_until = _parse_date(effective_until)
        if clear_target_account:
            item.target_account = None
        elif target_account is not None:
            item.target_account = _resolve_account(session, target_account)
        if notes is not None:
            item.notes = notes
        if item.flow_type == FlowType.TRANSFER and item.target_account is None:
            raise ValueError("flow_type='Transfer' requires a target_account.")
        session.commit()
        return _budget_item_to_dict(item)
    finally:
        session.close()


@server.tool()
def delete_budget_item(item_id: int) -> dict:
    """Delete a budget item."""
    session = get_session()
    try:
        item = session.get(BudgetItem, item_id)
        if item is None:
            raise ValueError(f"No budget item with id {item_id}")
        desc = item.description
        session.delete(item)
        session.commit()
        return {"deleted": desc}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# upcoming expenses
# ---------------------------------------------------------------------------


@server.tool()
def list_upcoming_expenses(
    start_date: str | None = None, end_date: str | None = None, status: str | None = None
) -> list[dict]:
    """List one-off upcoming expenses, optionally filtered to a date range (ISO dates) and/or
    status ('Pending', 'Archived', or 'Needs Review' — omit for all).

    Needs Review means a statement covering this expense's date was imported but nothing in it
    matched — use reschedule_upcoming_expense or archive_upcoming_expense to resolve it.
    """
    session = get_session()
    try:
        query = session.query(UpcomingExpense)
        if start_date:
            query = query.filter(UpcomingExpense.date >= _parse_date(start_date))
        if end_date:
            query = query.filter(UpcomingExpense.date <= _parse_date(end_date))
        if status:
            query = query.filter(UpcomingExpense.status == _resolve_upcoming_status(status))
        return [_upcoming_to_dict(u) for u in query.order_by(UpcomingExpense.date).all()]
    finally:
        session.close()


@server.tool()
def create_upcoming_expense(
    date: str,
    description: str,
    amount: float,
    category: str,
    account: str,
    flow_type: str = "Expense",
    target_account: str | None = None,
) -> dict:
    """Add a one-off dated item (ISO date string).

    flow_type: 'Income', 'Expense', or 'Transfer' (defaults to Expense). amount
    is always positive; flow_type carries the direction. Category is created
    automatically if new; account must already exist.

    flow_type='Transfer' requires target_account (any other existing
    account) — the amount is projected as an outflow from `account` and an
    inflow to `target_account` on this date.
    """
    session = get_session()
    try:
        acc = _resolve_account(session, account)
        resolved_flow_type = _resolve_flow_type(flow_type)
        if resolved_flow_type == FlowType.TRANSFER and not target_account:
            raise ValueError("flow_type='Transfer' requires target_account.")
        expense = UpcomingExpense(
            date=_parse_date(date),
            description=description,
            amount=amount,
            flow_type=resolved_flow_type,
            category=get_or_create_category(session, category),
            account=acc,
            target_account=_resolve_account(session, target_account) if target_account else None,
        )
        session.add(expense)
        session.commit()
        return _upcoming_to_dict(expense)
    finally:
        session.close()


@server.tool()
def update_upcoming_expense(
    item_id: int,
    date: str | None = None,
    description: str | None = None,
    amount: float | None = None,
    category: str | None = None,
    account: str | None = None,
    flow_type: str | None = None,
    target_account: str | None = None,
    clear_target_account: bool = False,
) -> dict:
    """Update a one-off upcoming expense. Only pass the fields you want to change.

    Changing `date` on a Needs Review item resets it to Pending (editing it counts as
    reschedule-and-reconsider) — use reschedule_upcoming_expense for that explicitly, or
    archive_upcoming_expense if it should just be dropped instead.
    """
    session = get_session()
    try:
        expense = session.get(UpcomingExpense, item_id)
        if expense is None:
            raise ValueError(f"No upcoming expense with id {item_id}")
        if date is not None:
            expense.date = _parse_date(date)
            if expense.status == UpcomingExpenseStatus.NEEDS_REVIEW:
                expense.status = UpcomingExpenseStatus.PENDING
                expense.matched_transaction = None
                expense.last_seen_statement = None
        if description is not None:
            expense.description = description
        if amount is not None:
            expense.amount = amount
        if category is not None:
            expense.category = get_or_create_category(session, category)
        if account is not None:
            expense.account = _resolve_account(session, account)
        if flow_type is not None:
            expense.flow_type = _resolve_flow_type(flow_type)
        if clear_target_account:
            expense.target_account = None
        elif target_account is not None:
            expense.target_account = _resolve_account(session, target_account)
        if expense.flow_type == FlowType.TRANSFER and expense.target_account is None:
            raise ValueError("flow_type='Transfer' requires a target_account.")
        session.commit()
        return _upcoming_to_dict(expense)
    finally:
        session.close()


@server.tool()
def reschedule_upcoming_expense(item_id: int, new_date: str) -> dict:
    """Move a one-off expense to a new date and reset it to Pending. Use on a Needs Review item
    (scheduled but not matched by any imported statement) that actually happened later, or
    hasn't happened yet."""
    session = get_session()
    try:
        expense = session.get(UpcomingExpense, item_id)
        if expense is None:
            raise ValueError(f"No upcoming expense with id {item_id}")
        expense.date = _parse_date(new_date)
        expense.status = UpcomingExpenseStatus.PENDING
        expense.matched_transaction = None
        expense.last_seen_statement = None
        session.commit()
        return _upcoming_to_dict(expense)
    finally:
        session.close()


@server.tool()
def archive_upcoming_expense(item_id: int) -> dict:
    """Mark a one-off expense Archived by hand. Use on a Needs Review item that just isn't
    happening — no need to reschedule it."""
    session = get_session()
    try:
        expense = session.get(UpcomingExpense, item_id)
        if expense is None:
            raise ValueError(f"No upcoming expense with id {item_id}")
        expense.status = UpcomingExpenseStatus.ARCHIVED
        session.commit()
        return _upcoming_to_dict(expense)
    finally:
        session.close()


@server.tool()
def delete_upcoming_expense(item_id: int) -> dict:
    """Delete a one-off upcoming expense."""
    session = get_session()
    try:
        expense = session.get(UpcomingExpense, item_id)
        if expense is None:
            raise ValueError(f"No upcoming expense with id {item_id}")
        desc = expense.description
        session.delete(expense)
        session.commit()
        return {"deleted": desc}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# budget summary + cashflow forecast
# ---------------------------------------------------------------------------


@server.tool()
def get_budget_summary(as_of: str | None = None) -> dict:
    """Monthly-equivalent income/expense totals by category.

    Only includes budget items active on `as_of` (defaults to today). Every
    frequency is normalised to a per-month figure (e.g. a weekly item counts
    as amount * 52/12).
    """
    session = get_session()
    try:
        ref = _parse_date(as_of) or dt.date.today()
        rows = monthly_budget_summary(session, ref).to_dicts()
        total_income = sum(r["monthly_amount"] for r in rows if r["flow_type"] == "Income")
        total_expense = sum(r["monthly_amount"] for r in rows if r["flow_type"] == "Expense")
        return {
            "as_of": ref.isoformat(),
            "by_category": rows,
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "net_remaining": round(total_income - total_expense, 2),
        }
    finally:
        session.close()


@server.tool()
def get_cashflow_forecast(start_date: str, end_date: str | None = None, account: str | None = None) -> dict:
    """Daily cashflow forecast over a date range (any length — a week, several
    months, a year).

    start_date and end_date are ISO dates ('YYYY-MM-DD'). As a shortcut,
    start_date may instead be a month ('YYYY-MM') with end_date omitted, in
    which case the forecast covers that whole calendar month.

    If account is omitted, returns the combined forecast across every
    account (valid from the latest balance_as_of date among them, with a
    per-account balance breakdown too). Otherwise returns the forecast for
    that single account, starting from its current_balance / balance_as_of,
    including which days (if any) breach its low-balance threshold.
    """
    session = get_session()
    try:
        if end_date is None and len(start_date) == 7:
            year, month_num = (int(p) for p in start_date.split("-"))
            range_start = dt.date(year, month_num, 1)
            range_end = dt.date(year, month_num, calendar.monthrange(year, month_num)[1])
        else:
            range_start = _parse_date(start_date)
            range_end = _parse_date(end_date) if end_date else range_start

        if account is None:
            df = combined_daily_forecast(session, range_start, range_end)
            acc = None
        else:
            acc = _resolve_account(session, account)
            df = account_daily_forecast(session, acc, range_start, range_end)

        if df.height == 0:
            return {
                "start_date": range_start.isoformat(),
                "end_date": range_end.isoformat(),
                "account": account,
                "days": [],
                "note": "No data — the account's balance-as-of date is after this range.",
            }

        days = df.to_dicts()
        for day in days:
            day["date"] = day["date"].isoformat()

        warnings = []
        if acc is not None:
            warnings = [
                {**w, "date": w["date"].isoformat()}
                for w in low_balance_warnings(session, range_start, range_end)
                if w["account"] == acc.name
            ]

        return {
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
            "account": account or "All Accounts (combined)",
            "days": days,
            "closing_balance": days[-1]["balance"],
            "warnings": warnings,
        }
    finally:
        session.close()


@server.tool()
def get_low_balance_warnings(start_date: str, end_date: str) -> list[dict]:
    """Every (account, date) in the given range where the forecast balance dips below that account's threshold."""
    session = get_session()
    try:
        warnings = low_balance_warnings(session, _parse_date(start_date), _parse_date(end_date))
        return [{**w, "date": w["date"].isoformat()} for w in warnings]
    finally:
        session.close()


def main():
    server.run()


if __name__ == "__main__":
    main()
