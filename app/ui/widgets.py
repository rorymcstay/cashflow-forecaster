from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from app.ui import theme


class CollapsibleSection(QWidget):
    """A titled panel that expands/collapses on clicking its header — lets a
    screen with many sections show only the ones the user actually wants to
    look at right now, instead of everything stacked and squeezed at once.

    Use `.content_layout` to build the panel's body, and `set_title()` to
    update the header text later (e.g. to show a live count)."""

    def __init__(self, title: str, expanded: bool = True, parent=None):
        super().__init__(parent)
        self._title = title

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._toggle_btn = QPushButton()
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setChecked(expanded)
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setStyleSheet(
            "QPushButton { text-align: left; padding: 6px 2px; border: none; "
            f"background: transparent; font-weight: bold; font-size: 14px; color: {theme.TEXT}; }}"
            f"QPushButton:hover {{ color: {theme.ACCENT}; }}"
        )
        self._toggle_btn.clicked.connect(self._on_toggled)
        outer.addWidget(self._toggle_btn)

        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(4, 4, 0, 8)
        outer.addWidget(self.content)

        self.content.setVisible(expanded)
        self._update_button_text()

    def _update_button_text(self) -> None:
        arrow = "▾" if self._toggle_btn.isChecked() else "▸"
        self._toggle_btn.setText(f"{arrow}  {self._title}")

    def _on_toggled(self) -> None:
        self.content.setVisible(self._toggle_btn.isChecked())
        self._update_button_text()

    def set_title(self, title: str) -> None:
        self._title = title
        self._update_button_text()

    def set_expanded(self, expanded: bool) -> None:
        self._toggle_btn.setChecked(expanded)
        self._on_toggled()

    def is_expanded(self) -> bool:
        return self._toggle_btn.isChecked()


class AccountMultiSelect(QPushButton):
    """Button that opens a checkbox-list popup for selecting zero or more
    items — accounts by default, but any id/name collection (e.g.
    categories) via `noun`/`noun_plural`/`all_selected_label`. `checked_ids()`
    always returns the literal list of checked ids (filtering by every id is
    equivalent to no filter, so callers don't need a special case for "all
    checked").

    `label_mode` only changes the button's own text: "include" describes the
    checked items as the ones being shown (`all_selected_label`, "N accounts
    selected"); "exclude" describes them as the ones being left out ("No
    accounts excluded", "N accounts excluded").
    """

    selectionChanged = Signal()

    def __init__(
        self,
        parent=None,
        label_mode: str = "include",
        noun: str = "account",
        noun_plural: str = "accounts",
        all_selected_label: str | None = None,
    ):
        super().__init__(parent)
        self.label_mode = label_mode
        self.noun = noun
        self.noun_plural = noun_plural
        self.all_selected_label = all_selected_label or f"All {noun_plural.capitalize()} (combined)"
        self._names: dict[int, str] = {}
        self._order: list[int] = []
        self._checked: set[int] = set()
        self.clicked.connect(self._show_popup)

    def set_accounts(
        self,
        accounts: list,
        default_all_checked: bool = True,
        default_checked_ids: set[int] | None = None,
    ) -> None:
        """Repopulate from the current item list (anything with `.id`/`.name`
        — accounts, categories, ...), keeping any previously checked items
        that still exist. New (first-population) items default to
        `default_checked_ids` if given, otherwise all-checked or
        none-checked per `default_all_checked`."""
        previous = self._checked
        had_previous = bool(self._names)
        self._names = {a.id: a.name for a in accounts}
        self._order = [a.id for a in accounts]
        if had_previous:
            self._checked = {i for i in previous if i in self._names}
        elif default_checked_ids is not None:
            self._checked = {i for i in default_checked_ids if i in self._names}
        elif default_all_checked:
            self._checked = set(self._order)
        else:
            self._checked = set()
        self._update_label()

    def checked_ids(self) -> list[int]:
        return [i for i in self._order if i in self._checked]

    def set_checked_ids(self, ids: list[int]) -> None:
        """Programmatically replace the checked set (e.g. "filter to just
        this one item, picked elsewhere in the screen") and notify listeners
        exactly as if the user had done it via the popup."""
        self._checked = {i for i in ids if i in self._names}
        self._update_label()
        self.selectionChanged.emit()

    def _update_label(self) -> None:
        if self.label_mode == "exclude":
            if not self._checked:
                self.setText(f"No {self.noun_plural} excluded")
            elif self._checked == set(self._names):
                self.setText(f"All {self.noun_plural} excluded")
            else:
                self.setText(f"{len(self._checked)} {self.noun}(s) excluded")
            return

        if not self._checked:
            self.setText(f"No {self.noun_plural} selected")
        elif self._checked == set(self._names):
            self.setText(self.all_selected_label)
        elif len(self._checked) == 1:
            (only_id,) = self._checked
            self.setText(self._names[only_id])
        else:
            self.setText(f"{len(self._checked)} {self.noun_plural} selected")

    def _show_popup(self) -> None:
        popup = QWidget(self, Qt.WindowType.Popup)
        popup.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 6px;"
        )
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(4, 4, 4, 4)

        # Only worth showing once the list is long enough that scrolling to
        # find one item by eye stops being faster than typing a few letters.
        search_edit = None
        if len(self._order) > 8:
            search_edit = QLineEdit(popup)
            search_edit.setPlaceholderText(f"Search {self.noun_plural}…")
            search_edit.setClearButtonEnabled(True)
            layout.addWidget(search_edit)

        list_widget = QListWidget(popup)
        list_widget.setFrameShape(QListWidget.Shape.NoFrame)
        for account_id in self._order:
            item = QListWidgetItem(self._names[account_id])
            item.setData(Qt.ItemDataRole.UserRole, account_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if account_id in self._checked else Qt.CheckState.Unchecked
            )
            list_widget.addItem(item)

        def on_item_changed(item: QListWidgetItem) -> None:
            account_id = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked:
                self._checked.add(account_id)
            else:
                self._checked.discard(account_id)
            self._update_label()
            self.selectionChanged.emit()

        list_widget.itemChanged.connect(on_item_changed)
        layout.addWidget(list_widget)

        if search_edit is not None:

            def apply_filter(text: str) -> None:
                needle = text.strip().lower()
                for row in range(list_widget.count()):
                    item = list_widget.item(row)
                    item.setHidden(bool(needle) and needle not in item.text().lower())

            search_edit.textChanged.connect(apply_filter)

        clear_btn = QPushButton("Clear selection", popup)
        clear_btn.setFlat(True)
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet(
            f"text-align: left; color: {theme.TEXT_MUTED}; border: none; padding: 4px 2px;"
        )

        def clear_selection() -> None:
            if not self._checked:
                return
            # Block signals while unchecking every row so on_item_changed
            # doesn't fire (and re-emit selectionChanged) once per item —
            # update the model once and emit a single, final signal instead.
            list_widget.blockSignals(True)
            for row in range(list_widget.count()):
                list_widget.item(row).setCheckState(Qt.CheckState.Unchecked)
            list_widget.blockSignals(False)
            self._checked.clear()
            self._update_label()
            self.selectionChanged.emit()

        clear_btn.clicked.connect(clear_selection)
        layout.addWidget(clear_btn)

        popup.setMinimumWidth(max(self.width(), 220))
        popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        popup.show()
        if search_edit is not None:
            search_edit.setFocus()
