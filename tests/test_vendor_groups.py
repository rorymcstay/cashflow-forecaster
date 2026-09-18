import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, VendorGroup
from app.vendor_groups import (
    create_vendor_group,
    group_for_vendor_key,
    list_vendor_groups,
    update_vendor_group,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_create_vendor_group_stores_comma_separated_keys(session):
    group = create_vendor_group(session, "Rideshare", ["UBER", "UBER EATS", "BOLT"])
    session.commit()

    assert group.vendors == "UBER,UBER EATS,BOLT"
    assert group.vendor_list == ["UBER", "UBER EATS", "BOLT"]


def test_create_vendor_group_with_no_vendors(session):
    group = create_vendor_group(session, "Empty")
    session.commit()

    assert group.vendors is None
    assert group.vendor_list == []


def test_list_vendor_groups_orders_by_name(session):
    create_vendor_group(session, "Zeta", ["A"])
    create_vendor_group(session, "Alpha", ["B"])
    session.commit()

    assert [g.name for g in list_vendor_groups(session)] == ["Alpha", "Zeta"]


def test_update_vendor_group_replaces_name_and_vendors(session):
    group = create_vendor_group(session, "Rideshare", ["UBER"])
    session.commit()

    update_vendor_group(group, "Transport", ["UBER", "BOLT"])
    session.commit()

    assert group.name == "Transport"
    assert group.vendor_list == ["UBER", "BOLT"]


def test_group_for_vendor_key_finds_first_matching_group():
    groups = [
        VendorGroup(name="Rideshare", vendors="UBER,BOLT"),
        VendorGroup(name="Groceries", vendors="TESCO,ALDI"),
    ]

    assert group_for_vendor_key(groups, "TESCO") is groups[1]
    assert group_for_vendor_key(groups, "AMAZON") is None
