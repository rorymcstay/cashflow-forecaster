import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Account, Base, Statement
from app.statement_import import find_statement_gaps


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_find_statement_gaps_flags_uncovered_months(session):
    account = Account(name="Personal Amex")
    session.add(account)
    session.flush()
    session.add_all(
        [
            Statement(account=account, period_start=dt.date(2026, 4, 6), period_end=dt.date(2026, 5, 5)),
            # June is skipped entirely — no statement's period touches it.
            Statement(account=account, period_start=dt.date(2026, 7, 6), period_end=dt.date(2026, 8, 5)),
        ]
    )
    session.commit()

    gaps = find_statement_gaps(session, account.id, dt.date(2026, 4, 1), dt.date(2026, 8, 31))

    assert [g["month"] for g in gaps] == ["2026-06"]
    assert gaps[0]["label"] == "June 2026"


def test_find_statement_gaps_no_statements_flags_every_month(session):
    account = Account(name="Pension")
    session.add(account)
    session.flush()
    session.commit()

    gaps = find_statement_gaps(session, account.id, dt.date(2026, 1, 1), dt.date(2026, 3, 31))

    assert [g["month"] for g in gaps] == ["2026-01", "2026-02", "2026-03"]


def test_find_statement_gaps_full_coverage_is_empty(session):
    account = Account(name="Personal HSBC")
    session.add(account)
    session.flush()
    session.add(
        Statement(account=account, period_start=dt.date(2026, 4, 24), period_end=dt.date(2026, 6, 23))
    )
    session.commit()

    gaps = find_statement_gaps(session, account.id, dt.date(2026, 5, 1), dt.date(2026, 6, 1))

    assert gaps == []
