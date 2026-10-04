import datetime as dt
import itertools

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app import sync
from app.budgets import ensure_default_budget, get_active_budget, set_active_budget
from app.models import (
    Account,
    Base,
    Budget,
    BudgetItem,
    Category,
    FlowType,
    Frequency,
    GlobalOptions,
    Statement,
    Transaction,
    sync_log,
)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Deterministic, strictly increasing wall clock shared by every
    "device", so which of two writes is later is never left to chance."""
    ticks = itertools.count(1_000_000)
    monkeypatch.setattr(sync.time, "time", lambda: next(ticks) / 1000)


def make_device():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    ensure_default_budget(session)
    session.commit()
    return session


@pytest.fixture
def a():
    return make_device()


@pytest.fixture
def b():
    return make_device()


def sync_pair(local, remote) -> tuple[sync.MergeResult, sync.MergeResult]:
    """What the web client does: pull the peer's new entries, then push ours."""
    pulled = sync.merge_entries(local, sync.entries_since(remote, sync.version_vector(local)))
    local.commit()
    pushed = sync.merge_entries(remote, sync.entries_since(local, sync.version_vector(remote)))
    remote.commit()
    return pulled, pushed


def add_item(session, description="Rent", amount=1000.0, account="Current", category="Housing"):
    acct = session.query(Account).filter_by(name=account).one_or_none() or Account(name=account)
    cat = session.query(Category).filter_by(name=category).one_or_none() or Category(name=category)
    item = BudgetItem(
        budget=get_active_budget(session),
        description=description,
        amount=amount,
        flow_type=FlowType.EXPENSE,
        frequency=Frequency.MONTHLY,
        effective_from=dt.date(2026, 1, 1),
        category=cat,
        account=acct,
    )
    session.add(item)
    session.commit()
    return item


def item_on(session, description) -> BudgetItem:
    return session.query(BudgetItem).filter_by(description=description).one()


def test_new_rows_replicate_with_foreign_keys(a, b):
    add_item(a, "Rent", 1000.0, account="Current", category="Housing")

    sync_pair(b, a)

    item = item_on(b, "Rent")
    assert item.amount == 1000.0
    assert item.account.name == "Current"
    assert item.category.name == "Housing"
    assert item.frequency == Frequency.MONTHLY
    assert item.effective_from == dt.date(2026, 1, 1)
    assert item.budget.name == "Default"
    # Both devices' independently-created "Default" budgets fold into one.
    assert b.query(Budget).count() == 1
    assert b.query(GlobalOptions).count() == 1


def test_concurrent_edits_to_different_columns_both_survive(a, b):
    add_item(a, "Rent")
    sync_pair(b, a)

    item_on(a, "Rent").amount = 1100.0
    a.commit()
    item_on(b, "Rent").notes = "Renewed lease"
    b.commit()
    sync_pair(b, a)

    for session in (a, b):
        item = item_on(session, "Rent")
        assert (item.amount, item.notes) == (1100.0, "Renewed lease")


def test_latest_write_wins_on_same_column(a, b):
    add_item(a, "Rent")
    sync_pair(b, a)

    item_on(b, "Rent").amount = 1200.0
    b.commit()
    item_on(a, "Rent").amount = 1300.0  # later
    a.commit()
    # Direction of the sync doesn't matter — both converge on the later write.
    sync_pair(b, a)

    assert item_on(a, "Rent").amount == 1300.0
    assert item_on(b, "Rent").amount == 1300.0


def test_write_after_sync_beats_peer_with_fast_clock(a, b, monkeypatch):
    add_item(a, "Rent")
    sync_pair(b, a)
    real_time = sync.time.time
    monkeypatch.setattr(sync.time, "time", lambda: real_time() + 3600)  # a's clock an hour ahead
    item_on(a, "Rent").amount = 1500.0
    a.commit()
    monkeypatch.setattr(sync.time, "time", real_time)
    sync_pair(b, a)

    # b's edit happens after it has seen a's — it must win despite a lower wall clock.
    item_on(b, "Rent").amount = 1600.0
    b.commit()
    sync_pair(b, a)

    assert item_on(a, "Rent").amount == 1600.0


def test_delete_replicates_and_is_final(a, b):
    add_item(a, "Rent")
    sync_pair(b, a)

    a.delete(item_on(a, "Rent"))
    a.commit()
    item_on(b, "Rent").amount = 999.0  # later edit of a row deleted elsewhere
    b.commit()
    sync_pair(b, a)

    assert a.query(BudgetItem).count() == 0
    assert b.query(BudgetItem).count() == 0


def test_cascade_deletes_replicate(a, b):
    acct = Account(name="Current")
    statement = Statement(account=acct, period_start=dt.date(2026, 1, 1), period_end=dt.date(2026, 1, 31))
    statement.transactions.append(Transaction(date=dt.date(2026, 1, 2), description="Coffee", amount=-3.0))
    a.add(statement)
    a.commit()
    sync_pair(b, a)
    assert b.query(Transaction).one().statement.account.name == "Current"

    a.delete(statement)
    a.commit()
    sync_pair(b, a)

    assert b.query(Statement).count() == 0
    assert b.query(Transaction).count() == 0


def test_independently_created_names_merge_instead_of_duplicating(a, b):
    add_item(a, "Rent", category="Housing", account="Current")
    add_item(b, "Council Tax", category="Housing", account="Current")

    sync_pair(b, a)

    for session in (a, b):
        assert session.query(Category).filter_by(name="Housing").count() == 1
        assert session.query(Account).filter_by(name="Current").count() == 1
        assert {i.description for i in session.query(BudgetItem)} == {"Rent", "Council Tax"}
        assert {i.category.name for i in session.query(BudgetItem)} == {"Housing"}

    # Later edits to the merged row, made under either device's identity,
    # still reach the other side.
    b.query(Account).filter_by(name="Current").one().low_balance_threshold = 50.0
    b.commit()
    sync_pair(a, b)
    assert a.query(Account).filter_by(name="Current").one().low_balance_threshold == 50.0


def test_rename_into_existing_name_keeps_local_value(a, b):
    a.add_all([Category(name="Food"), Category(name="Travel")])
    a.commit()
    sync_pair(b, a)
    b.add(Category(name="Groceries"))
    b.commit()
    a.query(Category).filter_by(name="Food").one().name = "Groceries"
    a.commit()

    pulled, _ = sync_pair(b, a)

    assert sorted(c.name for c in b.query(Category)) == ["Food", "Groceries", "Travel"]
    assert pulled.notes


def test_active_budget_selection_syncs(a, b):
    a.add(Budget(name="Lean"))
    a.commit()
    set_active_budget(a, a.query(Budget).filter_by(name="Lean").one().id)
    a.commit()

    sync_pair(b, a)

    assert get_active_budget(b).name == "Lean"


def test_fresh_device_defaults_dont_clobber_existing_data(a):
    a.add(Budget(name="Lean"))
    a.commit()
    set_active_budget(a, a.query(Budget).filter_by(name="Lean").one().id)
    a.commit()
    b = make_device()  # brand-new install: its own "Default" budget, active

    sync_pair(b, a)

    assert get_active_budget(a).name == "Lean"
    assert get_active_budget(b).name == "Lean"

    # ...but a deliberate choice made on the new device afterwards still wins.
    set_active_budget(b, b.query(Budget).filter_by(name="Default").one().id)
    b.commit()
    sync_pair(b, a)
    assert get_active_budget(a).name == "Default"


def test_resync_is_a_no_op_and_changes_relay_through_a_third_device(a, b):
    c = make_device()
    add_item(a, "Rent")
    sync_pair(b, a)

    pulled, pushed = sync_pair(b, a)
    assert (pulled.received, pushed.received) == (0, 0)

    sync_pair(c, b)  # c never talks to a directly
    assert item_on(c, "Rent").account.name == "Current"


def test_backfill_gives_existing_rows_a_baseline(a, b):
    add_item(a, "Rent")
    # Simulate a database from before sync existed.
    engine = a.get_bind()
    with engine.begin() as conn:
        conn.execute(delete(sync_log))
        for name in sync.synced_tables():
            conn.exec_driver_sql(f"DROP INDEX ix_{name}_sync_id")
            conn.exec_driver_sql(f"ALTER TABLE {name} DROP COLUMN sync_id")
    a.close()

    sync.backfill(engine)

    with engine.connect() as conn:
        assert conn.execute(select(sync_log)).first() is not None
    sync_pair(b, a)
    assert item_on(b, "Rent").account.name == "Current"
    # Running again on an up-to-date database adds nothing.
    with engine.connect() as conn:
        before = len(conn.execute(select(sync_log)).all())
    sync.backfill(engine)
    with engine.connect() as conn:
        assert len(conn.execute(select(sync_log)).all()) == before
