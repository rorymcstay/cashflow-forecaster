from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from sqlalchemy.orm import Session

from app.models import VendorGroup
from app.ui import theme
from app.ui.crud_screen import CrudScreen
from app.ui.dialogs import VendorGroupDialog
from app.vendor_group_recommender import VendorGroupSuggestion, recommend_vendor_groups
from app.vendor_groups import create_vendor_group, list_vendor_groups, update_vendor_group

COLUMNS = [
    ("Name", lambda g: g.name),
    ("Vendors", lambda g: ", ".join(g.vendor_list) or "—"),
    ("Vendor Count", lambda g: str(len(g.vendor_list)), lambda g: len(g.vendor_list)),
]


def query_vendor_groups(session: Session) -> list[VendorGroup]:
    return list_vendor_groups(session)


class VendorGroupsScreen(QWidget):
    """Vendor Groups management, plus a suggestion engine above it: finds
    recurring vendors that share a classified category and aren't in any
    group yet, and proposes a group per category for one-click accept."""

    def __init__(self, session: Session, on_change=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.on_change = on_change
        self._suggestions: list[VendorGroupSuggestion] = []

        layout = QVBoxLayout(self)

        suggest_row = QHBoxLayout()
        suggest_btn = QPushButton("🔮 Suggest Vendor Groups")
        suggest_btn.clicked.connect(self._suggest)
        suggest_row.addWidget(suggest_btn)
        hint = QLabel("Finds recurring vendors that share a category and aren't in any vendor group yet.")
        hint.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        suggest_row.addWidget(hint, stretch=1)
        layout.addLayout(suggest_row)

        self.suggestions_container = QWidget()
        self.suggestions_layout = QVBoxLayout(self.suggestions_container)
        self.suggestions_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.suggestions_container)
        self._suggestion_rows: dict[int, QWidget] = {}

        self.crud = CrudScreen(
            session,
            "Vendor Groups",
            COLUMNS,
            query_vendor_groups,
            VendorGroupDialog,
            on_change=self._on_crud_change,
            parent=self,
        )
        layout.addWidget(self.crud, stretch=1)

    def refresh(self):
        self.crud.refresh()

    def _on_crud_change(self):
        if self.on_change:
            self.on_change()

    # -- suggestions ---------------------------------------------------------

    def _suggest(self):
        self._suggestions = recommend_vendor_groups(self.session)
        self._render_suggestions()

    def _clear_suggestions_layout(self):
        while self.suggestions_layout.count():
            item = self.suggestions_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._suggestion_rows = {}

    def _render_suggestions(self):
        self._clear_suggestions_layout()
        for suggestion in self._suggestions:
            row_widget = self._build_suggestion_row(suggestion)
            self.suggestions_layout.addWidget(row_widget)
            self._suggestion_rows[id(suggestion)] = row_widget

    def _build_suggestion_row(self, suggestion: VendorGroupSuggestion) -> QWidget:
        row_widget = QWidget()
        row_widget.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 6px;"
        )
        row = QHBoxLayout(row_widget)
        label = QLabel(f"<b>🔮 {suggestion.name}</b>: {', '.join(suggestion.vendor_labels)}")
        label.setWordWrap(True)
        label.setToolTip(suggestion.rationale)
        row.addWidget(label, stretch=1)
        accept_btn = QPushButton("Accept")
        accept_btn.clicked.connect(lambda _checked=False, s=suggestion: self._accept(s))
        row.addWidget(accept_btn)
        dismiss_btn = QPushButton("Dismiss")
        dismiss_btn.clicked.connect(lambda _checked=False, s=suggestion: self._dismiss(s))
        row.addWidget(dismiss_btn)
        return row_widget

    def _remove_suggestion_row(self, suggestion: VendorGroupSuggestion) -> None:
        # Removing/deleting only this one row (not rebuilding the whole
        # panel) matters because this runs from inside that row's own
        # Accept/Dismiss button's clicked handler — tearing down every
        # sibling row (and reconnecting fresh signals for them) while one of
        # their own signal emissions is still on the call stack is exactly
        # the kind of Qt widget-lifecycle hazard that segfaults
        # intermittently rather than raising a catchable Python exception.
        self._suggestions = [s for s in self._suggestions if s is not suggestion]
        row_widget = self._suggestion_rows.pop(id(suggestion), None)
        if row_widget is not None:
            self.suggestions_layout.removeWidget(row_widget)
            row_widget.setParent(None)
            row_widget.deleteLater()

    def _accept(self, suggestion: VendorGroupSuggestion):
        existing = self.session.query(VendorGroup).filter(VendorGroup.name == suggestion.name).one_or_none()
        if existing is not None:
            merged = sorted(set(existing.vendor_list) | set(suggestion.vendor_keys))
            update_vendor_group(existing, existing.name, merged)
        else:
            create_vendor_group(self.session, suggestion.name, suggestion.vendor_keys)
        self.session.commit()
        self._remove_suggestion_row(suggestion)
        self.crud.refresh()
        if self.on_change:
            self.on_change()

    def _dismiss(self, suggestion: VendorGroupSuggestion):
        self._remove_suggestion_row(suggestion)
