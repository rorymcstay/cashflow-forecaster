"""Multi-device sync via a replicated write log.

Every ORM insert/update/delete on a mapped table is captured — in the same
transaction as the write itself, by the mapper events below — as an entry in
`sync_log` (see app/models.py). Two devices sync by exchanging the log
entries the other hasn't seen yet (tracked per origin device with a version
vector) and replaying them with `merge_entries`.

Conflicts resolve as last-write-wins, per column: an incoming value is only
applied if no entry already in the local log wrote that same column of that
same row at a later (ts, origin) — see `_rank` for the one wrinkle, rows
created independently on two devices. `ts` is a hybrid logical clock — wall-clock
ms, but always past every entry already in the local log — so a write made
just after a sync is always "later" than everything that sync brought in,
even across devices whose clocks disagree a little. Deletes are final: once a
row has been deleted on any device, later edits to it are ignored.

Rows are identified across devices by their `sync_id` UUID, never their
integer `id` (only meaningful locally), and foreign keys travel in the log as
the referenced row's sync_id. When two devices independently create rows
that collide on a natural key (a unique name, or the GlobalOptions
singleton), the incoming one is folded into the local one via `sync_alias`
instead of being duplicated.
"""

import datetime as dt
import enum
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import (
    Column,
    Connection,
    Date,
    DateTime,
    Engine,
    Enum,
    Table,
    UniqueConstraint,
    and_,
    bindparam,
    delete,
    event,
    func,
    insert,
    inspect,
    not_,
    select,
    update,
)
from sqlalchemy.orm import Mapper, Session

from app.models import Base, sync_alias, sync_log, sync_state

# Bump if the shape of an exchanged entry ever changes, so mismatched
# versions refuse to sync rather than silently mis-merging.
PROTOCOL_VERSION = 1

_UNSYNCED_COLUMNS = {"id", "sync_id"}

# Tables that only ever hold one row app-wide: an incoming insert is always
# folded into the local row rather than creating a second one.
_SINGLETON_TABLES = {"global_options"}


def _table(mapper: Mapper) -> Table:
    return cast(Table, mapper.local_table)


def synced_tables() -> dict[str, Table]:
    return {_table(mapper).name: _table(mapper) for mapper in Base.registry.mappers}


def device_id(conn: Connection) -> str:
    """This device's identity in the sync log — created on first use."""
    found = conn.execute(select(sync_state.c.device_id)).scalar()
    if found is None:
        found = str(uuid.uuid4())
        conn.execute(insert(sync_state).values(id=1, device_id=found))
    return found


# --- Capturing local writes ---------------------------------------------------


def _foreign_table(column: Column) -> Table | None:
    for fk in column.foreign_keys:
        return fk.column.table
    return None


def _encode(conn: Connection, table: Table, values: dict[str, Any]) -> dict[str, Any]:
    """Column values -> JSON-safe log data, with FKs as the target's sync_id."""
    data = {}
    for name, value in values.items():
        column = table.c[name]
        target = _foreign_table(column)
        if value is None:
            data[name] = None
        elif target is not None:
            data[name] = conn.execute(select(target.c.sync_id).where(target.c.id == value)).scalar()
        elif isinstance(value, enum.Enum):
            data[name] = value.name
        elif isinstance(value, (dt.date, dt.datetime)):
            data[name] = value.isoformat()
        else:
            data[name] = value
    return data


def _append(conn: Connection, tbl: str, row_uid: str | None, op: str, data: dict | None) -> None:
    if row_uid is None:
        return  # row predates sync and hasn't been backfilled yet
    origin = device_id(conn)
    last_ts = conn.execute(select(func.max(sync_log.c.ts))).scalar() or 0
    last_seq = conn.execute(select(func.max(sync_log.c.seq)).where(sync_log.c.origin == origin)).scalar() or 0
    conn.execute(
        insert(sync_log).values(
            origin=origin,
            seq=last_seq + 1,
            ts=max(int(time.time() * 1000), last_ts + 1),
            tbl=tbl,
            row_uid=row_uid,
            op=op,
            data=None if data is None else json.dumps(data),
        )
    )


def _column_values(mapper: Mapper, obj, changed_only: bool) -> dict[str, Any]:
    state = inspect(obj)
    values = {}
    for attr in mapper.column_attrs:
        column = attr.columns[0]
        if column.name in _UNSYNCED_COLUMNS:
            continue
        if changed_only and not state.attrs[attr.key].history.has_changes():
            continue
        values[column.name] = state.dict.get(attr.key)
    return values


@event.listens_for(Base, "after_insert", propagate=True)
def _log_insert(mapper: Mapper, conn: Connection, obj) -> None:
    data = _encode(conn, _table(mapper), _column_values(mapper, obj, changed_only=False))
    _append(conn, _table(mapper).name, inspect(obj).dict.get("sync_id"), "insert", data)


@event.listens_for(Base, "after_update", propagate=True)
def _log_update(mapper: Mapper, conn: Connection, obj) -> None:
    values = _column_values(mapper, obj, changed_only=True)
    if values:
        data = _encode(conn, _table(mapper), values)
        _append(conn, _table(mapper).name, inspect(obj).dict.get("sync_id"), "update", data)


@event.listens_for(Base, "before_delete", propagate=True)
def _log_delete(mapper: Mapper, conn: Connection, obj) -> None:
    table = _table(mapper)
    row_uid = inspect(obj).dict.get("sync_id")
    if row_uid is None:
        row_uid = conn.execute(select(table.c.sync_id).where(table.c.id == obj.id)).scalar()
    _append(conn, table.name, row_uid, "delete", None)


# --- Backfill -------------------------------------------------------------------


def backfill(engine: Engine) -> None:
    """Bring a database from before sync existed up to date: add the sync_id
    column where missing, then give every row without one a fresh sync_id
    and a baseline "insert" log entry — so the rows that already exist
    replicate to a new device like anything written afterwards. A no-op on
    every later startup."""
    tables = [t for t in Base.metadata.sorted_tables if t.name in synced_tables()]
    with engine.begin() as conn:
        for table in tables:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
            if "sync_id" not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN sync_id VARCHAR(36)")
            conn.exec_driver_sql(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table.name}_sync_id ON {table.name} (sync_id)"
            )

        # Two passes — all sync_ids first, then log entries — so a row's FKs
        # can always be encoded, regardless of table order.
        pending: list[tuple[Table, list[int]]] = []
        for table in tables:
            ids = list(conn.execute(select(table.c.id).where(table.c.sync_id.is_(None))).scalars())
            if ids:
                conn.execute(
                    update(table).where(table.c.id == bindparam("row_id")).values(sync_id=bindparam("uid")),
                    [{"row_id": row_id, "uid": str(uuid.uuid4())} for row_id in ids],
                )
                pending.append((table, ids))
        for table, ids in pending:
            for start in range(0, len(ids), 500):
                rows = conn.execute(select(table).where(table.c.id.in_(ids[start : start + 500])))
                for row in rows.mappings():
                    values = {k: v for k, v in row.items() if k not in _UNSYNCED_COLUMNS}
                    _append(conn, table.name, row["sync_id"], "insert", _encode(conn, table, values))


# --- Exchanging entries ---------------------------------------------------------


def version_vector(session: Session) -> dict[str, int]:
    """{origin device: highest seq from it in the local log}."""
    rows = session.execute(
        select(sync_log.c.origin, func.max(sync_log.c.seq)).group_by(sync_log.c.origin)
    ).all()
    return {origin: seq for origin, seq in rows}


def entries_since(session: Session, seen: dict[str, int]) -> list[dict]:
    """Every local log entry not covered by the peer's version vector."""
    query = select(sync_log).order_by(sync_log.c.ts, sync_log.c.origin, sync_log.c.seq)
    for origin, seq in seen.items():
        query = query.where(not_(and_(sync_log.c.origin == origin, sync_log.c.seq <= seq)))
    return [
        {
            "origin": row.origin,
            "seq": row.seq,
            "ts": row.ts,
            "tbl": row.tbl,
            "row_uid": row.row_uid,
            "op": row.op,
            "data": None if row.data is None else json.loads(row.data),
        }
        for row in session.execute(query)
    ]


@dataclass
class MergeResult:
    received: int = 0  # entries new to this device
    applied: int = 0  # of those, how many changed local data
    notes: list[str] = field(default_factory=list)  # anything skipped that's worth telling the user


def merge_entries(session: Session, entries: list[dict]) -> MergeResult:
    """Replay a peer's log entries into the local database (in the caller's
    transaction — commit afterwards). Entries already in the local log are
    ignored, so merging the same batch twice is harmless."""
    conn = session.connection()
    tables = synced_tables()
    result = MergeResult()
    for entry in sorted(entries, key=lambda e: (e["ts"], e["origin"], e["seq"])):
        already = conn.execute(
            select(sync_log.c.id).where(sync_log.c.origin == entry["origin"], sync_log.c.seq == entry["seq"])
        ).first()
        if already:
            continue
        conn.execute(
            insert(sync_log).values(
                origin=entry["origin"],
                seq=entry["seq"],
                ts=entry["ts"],
                tbl=entry["tbl"],
                row_uid=entry["row_uid"],
                op=entry["op"],
                data=None if entry["data"] is None else json.dumps(entry["data"]),
            )
        )
        result.received += 1
        table = tables.get(entry["tbl"])
        if table is None:
            result.notes.append(f"Ignored changes to unknown table '{entry['tbl']}' (newer app version?).")
        elif _apply(conn, table, entry, result):
            result.applied += 1
    # Merged rows were written underneath the ORM — make sure nothing
    # already loaded in this session keeps showing the old values.
    session.expire_all()
    return result


def _resolve(conn: Connection, row_uid: str) -> str:
    local = conn.execute(select(sync_alias.c.local_uid).where(sync_alias.c.remote_uid == row_uid)).scalar()
    return local or row_uid


def _row_id(conn: Connection, table: Table, row_uid: str) -> int | None:
    return conn.execute(select(table.c.id).where(table.c.sync_id == row_uid)).scalar()


def _row_history(conn: Connection, tbl: str, local_uid: str) -> list:
    """Every log entry for a row, under its local sync_id or any alias of it."""
    uids = [
        local_uid,
        *conn.execute(select(sync_alias.c.remote_uid).where(sync_alias.c.local_uid == local_uid)).scalars(),
    ]
    query = select(sync_log.c.ts, sync_log.c.origin, sync_log.c.op, sync_log.c.data).where(
        sync_log.c.tbl == tbl, sync_log.c.row_uid.in_(uids)
    )
    return list(conn.execute(query))


def _decode(conn: Connection, table: Table, data: dict[str, Any]) -> dict[str, Any] | None:
    """Log data -> column values; None if a required FK target is missing
    here (e.g. it was deleted), in which case the entry can't be applied."""
    values = {}
    for name, value in data.items():
        if name not in table.c:
            continue  # column from a newer schema
        column = table.c[name]
        target = _foreign_table(column)
        if value is None:
            values[name] = None
        elif target is not None:
            found = _row_id(conn, target, _resolve(conn, value))
            if found is None and not column.nullable:
                return None
            values[name] = found
        elif isinstance(column.type, Enum):
            values[name] = column.type.enum_class[value]
        elif isinstance(column.type, DateTime):
            values[name] = dt.datetime.fromisoformat(value)
        elif isinstance(column.type, Date):
            values[name] = dt.date.fromisoformat(value)
        else:
            values[name] = value
    return values


def _unique_column_sets(table: Table) -> list[tuple[str, ...]]:
    sets = {(c.name,) for c in table.columns if c.unique and c.name != "sync_id"}
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            names = tuple(c.name for c in constraint.columns)
            if "sync_id" not in names:
                sets.add(names)
    return sorted(sets)


def _find_twin(conn: Connection, table: Table, values: dict[str, Any]) -> tuple[int, str] | None:
    """An existing local row the incoming insert should be folded into."""
    if table.name in _SINGLETON_TABLES:
        return conn.execute(select(table.c.id, table.c.sync_id).limit(1)).first()
    for names in _unique_column_sets(table):
        if all(name in values for name in names):
            twin = conn.execute(
                select(table.c.id, table.c.sync_id).where(*(table.c[n] == values[n] for n in names))
            ).first()
            if twin is not None:
                return twin
    return None


def _rank(op: str, ts: int, origin: str) -> tuple:
    """Which of two entries' values for a column should stand. Updates are
    last-write-wins, and any update beats any insert. Inserts only compete
    when two devices independently created the same row (see _find_twin) —
    there the *earliest* wins, so a brand-new install's boilerplate (its own
    "Default" budget, active budget selection, ...) never overwrites the
    long-standing values on the device it first syncs with."""
    if op == "update":
        return (1, ts, origin)
    return (0, -ts, origin)


def _apply(conn: Connection, table: Table, entry: dict, result: MergeResult) -> bool:
    local_uid = _resolve(conn, entry["row_uid"])
    row_id = _row_id(conn, table, local_uid)

    if entry["op"] == "delete":
        if row_id is None:
            return False
        conn.execute(delete(table).where(table.c.id == row_id))
        return True

    history = _row_history(conn, table.name, local_uid)
    if any(h.op == "delete" for h in history):
        return False  # deletes are final
    values = _decode(conn, table, entry["data"] or {})
    if values is None:
        return False

    if row_id is None:
        if entry["op"] != "insert":
            return False
        twin = _find_twin(conn, table, values)
        if twin is None:
            conn.execute(insert(table).values(sync_id=local_uid, **values))
            return True
        row_id, local_uid = twin
        conn.execute(
            insert(sync_alias).values(remote_uid=entry["row_uid"], tbl=table.name, local_uid=local_uid)
        )
        history = _row_history(conn, table.name, local_uid)

    # Drop any column a higher-ranked entry already in the log wrote.
    incoming = _rank(entry["op"], entry["ts"], entry["origin"])
    for h in history:
        if h.data and _rank(h.op, h.ts, h.origin) > incoming:
            for name in json.loads(h.data):
                values.pop(name, None)

    # A unique column (e.g. a rename) that now collides with a different row
    # here can't be applied — keep the local value rather than fail the sync.
    for names in _unique_column_sets(table):
        if not any(n in values for n in names):
            continue
        current = conn.execute(select(*(table.c[n] for n in names)).where(table.c.id == row_id)).one()
        candidate = {n: values.get(n, current[i]) for i, n in enumerate(names)}
        clash = conn.execute(
            select(table.c.id).where(table.c.id != row_id, *(table.c[n] == v for n, v in candidate.items()))
        ).first()
        if clash is not None:
            for n in names:
                values.pop(n, None)
            result.notes.append(
                f"Kept local {table.name} {', '.join(names)}: {candidate} already in use here."
            )

    if not values:
        return False
    conn.execute(update(table).where(table.c.id == row_id).values(**values))
    return True
