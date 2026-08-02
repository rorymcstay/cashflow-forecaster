from collections.abc import Callable

from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableView, QVBoxLayout, QWidget,
)
from sqlalchemy.orm import Session

from app.ui.filter_proxy import GroupFilterProxyModel
from app.ui.table_models import ObjectTableModel


class CrudScreen(QWidget):
    """Reusable table + Add/Edit/Delete screen backed by a shared SQLAlchemy session.

    Supports click-header sorting, type-to-filter (matches any column), and
    an optional "Group by" column that clusters rows together with banded
    backgrounds instead of the default per-row alternating colors.
    """

    def __init__(self, session: Session, title: str, columns: list[tuple[str, Callable]],
                 query_fn: Callable[[Session], list], dialog_cls, on_change: Callable | None = None,
                 parent=None):
        super().__init__(parent)
        self.session = session
        self.query_fn = query_fn
        self.dialog_cls = dialog_cls
        self.on_change = on_change

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<h2>{title}</h2>"))

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Filter:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Type to filter…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self.filter_edit, stretch=1)

        toolbar.addWidget(QLabel("Group by:"))
        self.group_combo = QComboBox()
        self.group_combo.addItem("No grouping", -1)
        for i, col in enumerate(columns):
            self.group_combo.addItem(col[0], i)
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        toolbar.addWidget(self.group_combo)
        layout.addLayout(toolbar)

        self.model = ObjectTableModel(columns)
        self.proxy = GroupFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(self.edit_item)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add")
        add_btn.setObjectName("primaryButton")
        edit_btn = QPushButton("Edit")
        delete_btn = QPushButton("Delete")
        add_btn.clicked.connect(self.add_item)
        edit_btn.clicked.connect(self.edit_item)
        delete_btn.clicked.connect(self.delete_item)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.refresh()

    def _on_filter_changed(self, text: str):
        self.proxy.set_filter_text(text)

    def _on_group_changed(self, _index: int):
        self.proxy.set_group_column(self.group_combo.currentData())
        self.table.viewport().update()

    def refresh(self):
        self.model.set_rows(self.query_fn(self.session))
        self.table.resizeColumnsToContents()

    def selected_object(self):
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        source_row = self.proxy.mapToSource(idx).row()
        return self.model.object_at(source_row)

    def _notify_change(self):
        self.refresh()
        if self.on_change:
            self.on_change()

    def add_item(self):
        dlg = self.dialog_cls(self.session, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._notify_change()

    def edit_item(self):
        obj = self.selected_object()
        if obj is None:
            QMessageBox.information(self, "No selection", "Select a row to edit first.")
            return
        dlg = self.dialog_cls(self.session, obj=obj, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._notify_change()

    def delete_item(self):
        obj = self.selected_object()
        if obj is None:
            QMessageBox.information(self, "No selection", "Select a row to delete first.")
            return
        reply = QMessageBox.question(self, "Confirm delete", "Delete the selected item?")
        if reply == QMessageBox.StandardButton.Yes:
            self.session.delete(obj)
            self.session.commit()
            self._notify_change()
