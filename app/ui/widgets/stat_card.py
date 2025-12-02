"""Compact stat display card widget."""

from __future__ import annotations
from PySide6 import QtWidgets


class StatCard(QtWidgets.QFrame):
    """Compact stat display card with label, value, and unit.
    
    Used for displaying live measurements like voltage, current, power.
    """
    
    def __init__(self, label: str, unit: str, color: str, parent=None):
        """Initialize stat card.
        
        Args:
            label: Label text (e.g., "VOLTAGE")
            unit: Unit text (e.g., "V")
            color: Color for the value text (hex string)
            parent: Parent widget
        """
        super().__init__(parent)
        self.setProperty("class", "stat-card")
        self.color = color
        self._setup_ui(label, unit)
    
    def _setup_ui(self, label: str, unit: str) -> None:
        """Set up the card layout."""
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        
        lbl = QtWidgets.QLabel(label)
        lbl.setProperty("class", "stat-label")
        layout.addWidget(lbl)
        
        value_layout = QtWidgets.QHBoxLayout()
        value_layout.setSpacing(4)
        
        self.value_label = QtWidgets.QLabel("--")
        self.value_label.setProperty("class", "stat-value")
        self.value_label.setStyleSheet(f"color: {self.color};")
        value_layout.addWidget(self.value_label)
        
        unit_label = QtWidgets.QLabel(unit)
        unit_label.setProperty("class", "stat-label")
        value_layout.addWidget(unit_label)
        value_layout.addStretch()
        
        layout.addLayout(value_layout)
    
    def set_value(self, value: float, decimals: int = 3) -> None:
        """Update the displayed value.
        
        Args:
            value: Numeric value to display
            decimals: Number of decimal places
        """
        self.value_label.setText(f"{value:.{decimals}f}")
    
    def set_color(self, color: str) -> None:
        """Update the value color.
        
        Args:
            color: New color (hex string)
        """
        self.color = color
        self.value_label.setStyleSheet(f"color: {self.color};")
