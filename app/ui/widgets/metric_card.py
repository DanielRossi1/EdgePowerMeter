"""Live metric tile: name, large value, unit and an optional secondary line."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...export.units import scale_for_mode
from .common import label


class MetricCard(QtWidgets.QFrame):
    def __init__(self, name: str, unit: str, color: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._unit = unit
        self._last = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)

        head = QtWidgets.QHBoxLayout()
        self.dot = QtWidgets.QLabel()
        self.dot.setFixedSize(8, 8)
        head.addWidget(self.dot)
        self.name = label(name.upper(), "MetricName")
        head.addWidget(self.name)
        head.addStretch()
        lay.addLayout(head)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.value = label("-", "MetricValue")
        self.value.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        row.addWidget(self.value)
        self.unit = label(unit, "MetricUnit")
        row.addWidget(self.unit, 0, QtCore.Qt.AlignBottom)
        row.addStretch()
        lay.addLayout(row)

        self.secondary = label("", "Hint")
        self.secondary.setVisible(False)
        lay.addWidget(self.secondary)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        self.dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        self.value.setStyleSheet(f"color: {color};")

    def set_value(self, value, mode: str = "auto", decimals: int = 4) -> None:
        if value is None:
            self.value.setText("-")
            self.unit.setText(self._unit)
            return
        number, unit = scale_for_mode(float(value), self._unit, mode, decimals)
        self.value.setText(number)
        self.unit.setText(unit)

    def set_text(self, text: str, unit: str = "") -> None:
        self.value.setText(text)
        self.unit.setText(unit)

    def set_secondary(self, text: str) -> None:
        self.secondary.setText(text)
        self.secondary.setVisible(bool(text))
