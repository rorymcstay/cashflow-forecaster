from sqlalchemy.orm import Session

from app.models import VendorGroup
from app.ui.crud_screen import CrudScreen
from app.ui.dialogs import VendorGroupDialog
from app.vendor_groups import list_vendor_groups

COLUMNS = [
    ("Name", lambda g: g.name),
    ("Vendors", lambda g: ", ".join(g.vendor_list) or "—"),
    ("Vendor Count", lambda g: str(len(g.vendor_list)), lambda g: len(g.vendor_list)),
]


def query_vendor_groups(session: Session) -> list[VendorGroup]:
    return list_vendor_groups(session)


class VendorGroupsScreen(CrudScreen):
    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(
            session,
            "Vendor Groups",
            COLUMNS,
            query_vendor_groups,
            VendorGroupDialog,
            on_change=on_change,
            parent=parent,
        )
