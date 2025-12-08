"""Compact vertical CPU usage bar widget."""

from __future__ import annotations

from PySide6 import QtWidgets, QtGui, QtCore


class CPUBar(QtWidgets.QWidget):
    """Displays CPU usage as a set of vertical bars.

    Designed for status bars: low height, compact width, no text overlay.
    """

    def __init__(self, bar_count: int = 6, parent=None):
        super().__init__(parent)
        self._usage: float = 0.0
        self._bar_count = max(3, bar_count)
        self._fill_color = QtGui.QColor("#4caf50")
        self._bg_color = QtGui.QColor(60, 60, 60, 120)
        self.setFixedHeight(28)
        self.setFixedWidth(self._bar_count * 8 + (self._bar_count - 1) * 2)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)

    def set_colors(self, fill: str, background: str) -> None:
        self._fill_color = QtGui.QColor(fill)
        self._bg_color = QtGui.QColor(background)
        self.update()

    def set_usage(self, usage: float) -> None:
        clamped = max(0.0, min(usage, 100.0))
        if abs(clamped - self._usage) < 0.1:
            return
        self._usage = clamped
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        w = self.width()
        h = self.height()
        bar_w = 6
        spacing = 2

        usage_ratio = self._usage / 100.0 if self._usage > 0 else 0.0

        for idx in range(self._bar_count):
            x = idx * (bar_w + spacing)
            # Subdivide usage across bars uniformly
            bar_height = int(h * usage_ratio)
            # Slight taper for a more modern look
            taper = max(0, self._bar_count - idx - 1)
            bar_height = max(2, bar_height - taper)
            y = h - bar_height

            # Background track
            painter.setBrush(self._bg_color)
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRoundedRect(x, 2, bar_w, h - 4, 2, 2)

            # Filled portion
            if bar_height > 2:
                painter.setBrush(self._fill_color)
                painter.drawRoundedRect(x, y, bar_w, bar_height - 2, 2, 2)

        painter.end()
