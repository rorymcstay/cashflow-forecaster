"""Vendor Groups: user-defined collections of vendors (merchant keys) that
act as an alternative axis to Category for reporting — see app/analytics.py,
which accepts either a category or a vendor group as the grouping dimension
for the Insights (moving-average vs forecast) screen.

Membership is stored the same way as BudgetItem.vendors (a comma-separated
list of normalised merchant keys — see app/transactions.py merchant_key), so
a vendor can belong to any number of groups just as it can appear in any
number of vendor-scoped budget lines.
"""

from sqlalchemy.orm import Session

from app.models import VendorGroup

# Re-exported so callers building a vendor picker (desktop + web Vendor
# Groups screens) use the exact same "distinct merchant, most-frequent
# label, occurrence count" logic already used by the Budget Builder —
# rather than a second definition of what a "vendor" is.
from app.budget_builder import VendorOption, vendor_options  # noqa: F401


def list_vendor_groups(session: Session) -> list[VendorGroup]:
    return session.query(VendorGroup).order_by(VendorGroup.name).all()


def create_vendor_group(session: Session, name: str, vendor_keys: list[str] | None = None) -> VendorGroup:
    group = VendorGroup(name=name)
    group.vendor_list = vendor_keys or []
    session.add(group)
    session.flush()
    return group


def update_vendor_group(group: VendorGroup, name: str, vendor_keys: list[str] | None) -> VendorGroup:
    group.name = name
    group.vendor_list = vendor_keys or []
    return group


def group_for_vendor_key(vendor_groups: list[VendorGroup], key: str) -> VendorGroup | None:
    """The first vendor group (if any) whose membership includes this
    merchant key — used wherever a single transaction needs to be filed
    under a group, e.g. a spend breakdown grouped by vendor group instead of
    category."""
    return next((g for g in vendor_groups if key in g.vendor_list), None)
