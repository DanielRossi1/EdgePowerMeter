"""Small reusable building blocks for the pages."""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from ..icons import icon


def button(text: str, variant: str = "", icon_name: str = "", icon_color: str = "",
           tooltip: str = "") -> QtWidgets.QPushButton:
    b = QtWidgets.QPushButton(text)
    if variant:
        b.setProperty("variant", variant)
    if icon_name and icon_color:
        b.setIcon(icon(icon_name, icon_color, 18))
    if tooltip:
        b.setToolTip(tooltip)
    b.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
    return b


def set_variant(widget: QtWidgets.QWidget, variant: str) -> None:
    widget.setProperty("variant", variant)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def label(text: str = "", name: str = "", wrap: bool = False,
          selectable: bool = False) -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    if selectable:
        lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    return lab


class Card(QtWidgets.QFrame):
    """Rounded panel with an optional title, subtitle and body layout."""

    def __init__(self, title: str = "", subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(10)
        if title or subtitle:
            head = QtWidgets.QVBoxLayout()
            head.setSpacing(2)
            if title:
                head.addWidget(label(title, "CardTitle"))
            if subtitle:
                head.addWidget(label(subtitle, "Hint", wrap=True))
            outer.addLayout(head)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)


class FormGrid(QtWidgets.QGridLayout):
    """Label / control rows with an optional explanation under the control."""

    def __init__(self):
        super().__init__()
        self.setHorizontalSpacing(16)
        self.setVerticalSpacing(10)
        self.setColumnStretch(1, 1)
        self._row = 0

    def add(self, text: str, widget: QtWidgets.QWidget, hint: str = "") -> QtWidgets.QLabel:
        if isinstance(widget, (QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox)):
            # Compact controls; long free-text fields keep the full width.
            widget.setMinimumWidth(240)
            widget.setMaximumWidth(380)
            holder = QtWidgets.QWidget()
            hl = QtWidgets.QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.addWidget(widget)
            hl.addStretch()
            widget = holder
        lab = label(text)
        lab.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.addWidget(lab, self._row, 0, QtCore.Qt.AlignTop if hint else QtCore.Qt.AlignVCenter)
        if hint:
            box = QtWidgets.QVBoxLayout()
            box.setSpacing(3)
            box.addWidget(widget)
            box.addWidget(label(hint, "Hint", wrap=True))
            self.addLayout(box, self._row, 1)
        else:
            self.addWidget(widget, self._row, 1)
        self._row += 1
        return lab

    def add_full(self, widget: QtWidgets.QWidget, hint: str = "") -> None:
        if hint:
            box = QtWidgets.QVBoxLayout()
            box.setSpacing(3)
            box.addWidget(widget)
            box.addWidget(label(hint, "Hint", wrap=True))
            self.addLayout(box, self._row, 0, 1, 2)
        else:
            self.addWidget(widget, self._row, 0, 1, 2)
        self._row += 1


class KeyValueList(QtWidgets.QWidget):
    """Two-column read-only key/value list with updatable values."""

    def __init__(self, keys: Sequence[Tuple[str, str]], parent=None):
        super().__init__(parent)
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        # Labels get the width (they wrap when needed); values are short.
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)
        self._values = {}
        for row, (key, text) in enumerate(keys):
            grid.addWidget(label(text, "KeyLabel", wrap=True), row, 0)
            val = label("-", "KeyValue", selectable=True)
            val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            grid.addWidget(val, row, 1)
            self._values[key] = val

    def set(self, key: str, text: str, color: str = "") -> None:
        lab = self._values[key]
        lab.setText(text)
        lab.setStyleSheet(f"color: {color};" if color else "")


def combo(items: Iterable[Tuple[str, object]], current: object = None) -> QtWidgets.QComboBox:
    c = QtWidgets.QComboBox()
    for text, data in items:
        c.addItem(text, data)
    if current is not None:
        idx = c.findData(current)
        if idx >= 0:
            c.setCurrentIndex(idx)
    return c


def select_data(c: QtWidgets.QComboBox, data: object) -> None:
    idx = c.findData(data)
    if idx >= 0 and idx != c.currentIndex():
        c.blockSignals(True)
        c.setCurrentIndex(idx)
        c.blockSignals(False)


def spin(lo: float, hi: float, value: float, decimals: int = 0, step: float = 1,
         suffix: str = "") -> QtWidgets.QAbstractSpinBox:
    if decimals == 0:
        s = QtWidgets.QSpinBox()
        s.setRange(int(lo), int(hi))
        s.setValue(int(value))
        s.setSingleStep(int(step))
    else:
        s = QtWidgets.QDoubleSpinBox()
        s.setDecimals(decimals)
        s.setRange(lo, hi)
        s.setValue(value)
        s.setSingleStep(step)
    if suffix:
        s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    s.setMinimumWidth(110)
    return s


def scroll_page(content: QtWidgets.QWidget, max_width: int = 980) -> QtWidgets.QScrollArea:
    """Wrap a page body in a scroll area, centered with a comfortable max width."""
    area = QtWidgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QtWidgets.QFrame.NoFrame)
    holder = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(holder)
    lay.setContentsMargins(20, 16, 20, 20)
    lay.addStretch(0)
    content.setMaximumWidth(max_width)
    lay.addWidget(content, 1)
    lay.addStretch(0)
    area.setWidget(holder)
    return area


class Banner(QtWidgets.QFrame):
    """Dismissable inline message bar."""

    def __init__(self, text: str, color: str, text_color: str = "#1a1a1a", parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        self.setStyleSheet(f"QFrame#Banner {{ background: {color}; }}")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 8)
        self.text = label(text, wrap=True)
        self.text.setStyleSheet(f"color: {text_color}; font-weight: 500;")
        lay.addWidget(self.text, 1)
        close = QtWidgets.QToolButton()
        close.setIcon(icon("close", text_color, 16))
        close.setAutoRaise(True)
        close.setStyleSheet("border: none;")
        close.clicked.connect(self.hide)
        lay.addWidget(close)


def confirm(parent: Optional[QtWidgets.QWidget], title: str, text: str,
            yes: str, no: str) -> bool:
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Question)
    box.setWindowTitle(title)
    box.setText(text)
    yes_btn = box.addButton(yes, QtWidgets.QMessageBox.AcceptRole)
    box.addButton(no, QtWidgets.QMessageBox.RejectRole)
    box.setDefaultButton(yes_btn)
    box.exec()
    return box.clickedButton() is yes_btn
