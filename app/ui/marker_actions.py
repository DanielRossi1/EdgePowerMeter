"""Marker interactions shared by the Live and Analysis plots."""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtWidgets

from ..i18n import tr
from .controller import AcquisitionController
from .widgets.common import confirm


class MarkerActions(QtCore.QObject):
    """Add / rename / move / delete markers, with the same gestures everywhere:

    * key M or the Marker button: marker at the current live time;
    * right-click on a plot: menu to add a marker exactly there, or to rename
      or delete the marker under the cursor;
    * double-click on a marker: rename it;
    * drag a marker (Analysis plot): adjust its time.
    """

    status = QtCore.Signal(str)

    def __init__(self, ctrl: AcquisitionController, parent_widget: QtWidgets.QWidget):
        super().__init__(parent_widget)
        self.ctrl = ctrl
        self.parent_widget = parent_widget

    def attach(self, plot) -> None:
        plot.context_requested.connect(self._context_menu)
        plot.marker_activated.connect(self.rename)
        plot.marker_moved.connect(self._moved)

    # ------------------------------------------------------------- actions

    def add_now(self) -> None:
        if not self.ctrl.is_running:
            self.status.emit(tr("Right-click on a plot to place a marker at a precise point."))
            return
        m = self.ctrl.add_marker()
        if m is not None:
            self.status.emit(tr("Marker {label} added at {time}. Double-click it to rename.",
                                label=m.label, time=f"{m.t:.3f} s"))

    def add_at(self, t: float) -> None:
        m = self.ctrl.add_marker(t)
        if m is not None:
            self.status.emit(tr("Marker {label} added at {time}.", label=m.label, time=f"{m.t:.3f} s"))

    def rename(self, marker_id: int) -> None:
        m = self.ctrl.markers.get(marker_id)
        if m is None:
            return
        text, ok = QtWidgets.QInputDialog.getText(
            self.parent_widget, tr("Rename marker"),
            tr("Name of the marker at {time}:", time=f"{m.t:.3f} s"),
            QtWidgets.QLineEdit.Normal, m.label)
        if ok and text.strip():
            self.ctrl.rename_marker(marker_id, text)

    def delete(self, marker_id: int) -> None:
        self.ctrl.remove_marker(marker_id)

    def delete_all(self) -> None:
        if len(self.ctrl.markers) and confirm(
                self.parent_widget, tr("Delete markers"),
                tr("Delete all {n} markers?", n=len(self.ctrl.markers)),
                tr("Delete"), tr("Cancel")):
            self.ctrl.clear_markers()

    def _moved(self, marker_id: int, t: float) -> None:
        self.ctrl.move_marker(marker_id, t)

    def _context_menu(self, t: float, marker_id: int, global_pos) -> None:
        if not len(self.ctrl.store):
            return
        menu = QtWidgets.QMenu(self.parent_widget)
        add = menu.addAction(tr("Add marker here ({time})", time=f"{t:.3f} s"))
        add.triggered.connect(lambda: self.add_at(t))
        m: Optional[object] = self.ctrl.markers.get(marker_id) if marker_id else None
        if m is not None:
            menu.addSeparator()
            menu.addAction(tr("Rename \"{label}\"...", label=m.label)).triggered.connect(
                lambda: self.rename(marker_id))
            menu.addAction(tr("Delete \"{label}\"", label=m.label)).triggered.connect(
                lambda: self.delete(marker_id))
        if len(self.ctrl.markers):
            menu.addSeparator()
            menu.addAction(tr("Delete all markers")).triggered.connect(self.delete_all)
        menu.exec(global_pos)
