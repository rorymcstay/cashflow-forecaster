import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

DB_PATH = (
    Path(os.environ["BUDGETING_DB_PATH"])
    if "BUDGETING_DB_PATH" in os.environ
    else (Path(__file__).resolve().parent.parent / "budgeting.db")
)
# Generous pool headroom: the desktop app and MCP server each hold exactly
# one session for their whole lifetime, but the web UI opens one session per
# browser tab (closed on disconnect) — several tabs, or a slow-to-connect
# client, can otherwise exhaust a small default pool.
engine = create_engine(f"sqlite:///{DB_PATH}", pool_size=20, max_overflow=40)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    # WAL + a busy timeout so the GUI app and the MCP server can safely hit
    # the same SQLite file at the same time without "database is locked" errors.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()
    _drop_removed_columns()
    _migrate_holdings_to_quantity()
    _migrate_budgets()


_ADDED_COLUMNS = {
    "upcoming_expenses": [
        ("flow_type", "VARCHAR(7) NOT NULL DEFAULT 'EXPENSE'"),
        ("status", "VARCHAR(12) NOT NULL DEFAULT 'PENDING'"),
        ("matched_transaction_id", "INTEGER REFERENCES transactions(id)"),
        ("last_seen_statement_id", "INTEGER REFERENCES statements(id)"),
        ("target_account_id", "INTEGER REFERENCES accounts(id)"),
    ],
    "accounts": [
        ("growth_rate", "FLOAT"),
        ("is_credit_card", "BOOLEAN NOT NULL DEFAULT 0"),
        ("cc_payee_account_id", "INTEGER REFERENCES accounts(id)"),
        ("cc_payment_day", "INTEGER"),
        ("cc_pay_in_full", "BOOLEAN NOT NULL DEFAULT 0"),
        ("cc_fixed_payment_amount", "FLOAT"),
        ("cash_position", "FLOAT NOT NULL DEFAULT 0"),
    ],
    "budget_items": [
        ("target_account_id", "INTEGER REFERENCES accounts(id)"),
        ("vendors", "VARCHAR(2000)"),
        # Nullable at the DB level purely so this ALTER works against
        # existing rows — _migrate_budgets() backfills every row to the
        # default budget immediately after, so it's never actually null in
        # practice. The model's BudgetItem.budget_id stays non-Optional.
        ("budget_id", "INTEGER REFERENCES budgets(id)"),
    ],
    "holdings": [
        ("average_price", "FLOAT"),
    ],
}

# Columns added in an earlier revision of the schema that no longer exist on
# the model — dropped so stale data doesn't linger in the wild.
_REMOVED_COLUMNS = {
    "accounts": ["is_savings"],
}


def _add_missing_columns() -> None:
    """create_all only adds new tables, not new columns on existing ones —
    patch columns added after a table already existed in the wild."""
    with engine.connect() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        conn.commit()


def _drop_removed_columns() -> None:
    with engine.connect() as conn:
        for table, columns in _REMOVED_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name in columns:
                if name in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {name}")
        conn.commit()


def _migrate_holdings_to_quantity() -> None:
    """One-time migration: holdings.weight (relative portfolio weight,
    manually set) -> holdings.quantity (actual share count) — see
    app/investments.py. Runs only while the old `weight` column still
    exists (a no-op forever after, on every later startup).

    Back-computes each holding's share count from its account's
    pre-migration current_balance (the combined cash+shares total under the
    old model) times its weight, divided by that ticker's live price — i.e.
    assumes weight summed to 1 (no leftover cash) unless a ticker's price
    can't be fetched. If ANY ticker's price is unavailable (no network),
    the whole migration is skipped and retried on the next startup rather
    than guessing — better to wait than to write a wrong share count into
    real financial data.
    """
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(holdings)")}
        if "weight" not in existing:
            return
        rows = conn.exec_driver_sql(
            "SELECT h.id, h.ticker, h.weight, a.current_balance "
            "FROM holdings h JOIN accounts a ON a.id = h.account_id"
        ).fetchall()
        tickers = sorted({row[1] for row in rows})
        prices = {}
        if tickers:
            from app import market_data

            prices = market_data.fetch_latest_prices(tickers)
            if not prices:
                return  # no network / prices unavailable — retry next startup

        if "quantity" not in existing:
            conn.exec_driver_sql("ALTER TABLE holdings ADD COLUMN quantity FLOAT")
        for holding_id, ticker, weight, current_balance in rows:
            price = prices.get(ticker)
            # A ticker that couldn't be priced keeps its old weight number as
            # a placeholder quantity — clearly not a real share count, but
            # preserves the holding for manual correction instead of losing it.
            quantity = (current_balance * weight) / price if price else weight
            conn.exec_driver_sql("UPDATE holdings SET quantity = ? WHERE id = ?", (quantity, holding_id))
        conn.exec_driver_sql("ALTER TABLE holdings DROP COLUMN weight")
        conn.commit()


def _migrate_budgets() -> None:
    """Guarantee a default budget + active selection exist, and file any
    pre-multi-budget BudgetItem rows (budget_id still null after the ALTER
    above) under it."""
    from app.budgets import backfill_unassigned_budget_items, ensure_default_budget

    session = SessionLocal()
    try:
        budget = ensure_default_budget(session)
        backfill_unassigned_budget_items(session, budget)
        session.commit()
    finally:
        session.close()


def get_session() -> Session:
    return SessionLocal()
