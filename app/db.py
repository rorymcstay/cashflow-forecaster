from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

DB_PATH = Path(__file__).resolve().parent.parent / "budgeting.db"
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
    ],
    "budget_items": [
        ("target_account_id", "INTEGER REFERENCES accounts(id)"),
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


def get_session() -> Session:
    return SessionLocal()
