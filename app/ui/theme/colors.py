"""Theme colors and definitions for EdgePowerMeter UI."""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ThemeColors:
    """Color palette for a theme."""
    # Backgrounds
    bg_primary: str
    bg_secondary: str
    bg_card: str
    bg_elevated: str
    
    # Accents
    accent_primary: str
    accent_success: str
    accent_warning: str
    accent_danger: str
    accent_purple: str
    
    # Text
    text_primary: str
    text_secondary: str
    text_muted: str
    
    # Charts
    chart_voltage: str
    chart_current: str
    chart_power: str
    
    # Borders
    border_default: str
    border_hover: str


# Dark Theme (GitHub-inspired)
DARK_THEME = ThemeColors(
    bg_primary="#0d1117",
    bg_secondary="#161b22",
    bg_card="#21262d",
    bg_elevated="#30363d",
    accent_primary="#58a6ff",
    accent_success="#3fb950",
    accent_warning="#d29922",
    accent_danger="#f85149",
    accent_purple="#a371f7",
    text_primary="#f0f6fc",
    text_secondary="#8b949e",
    text_muted="#484f58",
    chart_voltage="#58a6ff",
    chart_current="#d29922",
    chart_power="#3fb950",
    border_default="#30363d",
    border_hover="#58a6ff",
)

# Light Theme (Clean, modern light theme)
LIGHT_THEME = ThemeColors(
    bg_primary="#f8fafc",
    bg_secondary="#ffffff",
    bg_card="#ffffff",
    bg_elevated="#e2e8f0",
    accent_primary="#2563eb",
    accent_success="#16a34a",
    accent_warning="#ca8a04",
    accent_danger="#dc2626",
    accent_purple="#7c3aed",
    text_primary="#0f172a",
    text_secondary="#475569",
    text_muted="#94a3b8",
    chart_voltage="#2563eb",
    chart_current="#ca8a04",
    chart_power="#16a34a",
    border_default="#cbd5e1",
    border_hover="#2563eb",
)


def generate_stylesheet(theme: ThemeColors) -> str:
    """Generate Qt stylesheet from theme colors."""
    return f"""
QMainWindow {{
    background-color: {theme.bg_primary};
}}

QWidget {{
    background-color: transparent;
    color: {theme.text_primary};
    font-family: 'Inter', 'SF Pro Display', 'Segoe UI', sans-serif;
    font-size: 13px;
}}

QLabel {{
    color: {theme.text_primary};
}}

QLabel[class="title"] {{
    font-size: 24px;
    font-weight: 700;
}}

QLabel[class="subtitle"] {{
    font-size: 12px;
    color: {theme.text_secondary};
}}

QLabel[class="stat-value"] {{
    font-size: 24px;
    font-weight: 600;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
}}

QLabel[class="stat-label"] {{
    font-size: 10px;
    color: {theme.text_secondary};
    text-transform: uppercase;
}}

QPushButton {{
    background-color: {theme.bg_card};
    color: {theme.text_primary};
    border: 1px solid {theme.border_default};
    border-radius: 6px;
    padding: 6px 12px;
    font-weight: 500;
    font-size: 12px;
}}

QPushButton:hover {{
    background-color: {theme.bg_elevated};
    border-color: {theme.border_hover};
}}

QPushButton:disabled {{
    background-color: {theme.bg_secondary};
    color: {theme.text_muted};
}}

QPushButton[class="primary"] {{
    background-color: {theme.accent_primary};
    border-color: {theme.accent_primary};
    color: white;
}}

QPushButton[class="success"] {{
    background-color: {theme.accent_success};
    border-color: {theme.accent_success};
    color: white;
}}

QPushButton[class="danger"] {{
    background-color: transparent;
    border-color: {theme.accent_danger};
    color: {theme.accent_danger};
}}

QPushButton[class="danger"]:hover {{
    background-color: {theme.accent_danger};
    color: white;
}}

QPushButton[class="icon"] {{
    background-color: transparent;
    border: none;
    padding: 4px 8px;
    font-size: 16px;
}}

QPushButton[class="icon"]:hover {{
    background-color: {theme.bg_elevated};
    border-radius: 4px;
}}

QComboBox {{
    background-color: {theme.bg_card};
    color: {theme.text_primary};
    border: 1px solid {theme.border_default};
    border-radius: 6px;
    padding: 6px 10px;
    min-width: 120px;
}}

QComboBox:hover {{
    border-color: {theme.border_hover};
}}

QComboBox::drop-down {{
    border: none;
}}

QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {theme.text_secondary};
}}

QComboBox QAbstractItemView {{
    background-color: {theme.bg_card};
    border: 1px solid {theme.border_default};
    selection-background-color: {theme.accent_primary};
}}

QCheckBox {{
    color: {theme.text_primary};
    spacing: 6px;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 3px;
    border: 1px solid {theme.border_default};
    background-color: {theme.bg_card};
}}

QCheckBox::indicator:checked {{
    background-color: {theme.accent_primary};
    border-color: {theme.accent_primary};
}}

QFrame[class="card"] {{
    background-color: {theme.bg_secondary};
    border: 1px solid {theme.border_default};
    border-radius: 8px;
}}

QFrame[class="stat-card"] {{
    background-color: {theme.bg_card};
    border: 1px solid {theme.border_default};
    border-radius: 6px;
}}

QScrollBar:vertical {{
    background-color: {theme.bg_primary};
    width: 8px;
}}

QScrollBar::handle:vertical {{
    background-color: {theme.bg_elevated};
    border-radius: 4px;
    min-height: 20px;
}}

QSlider::groove:horizontal {{
    height: 4px;
    background-color: {theme.bg_elevated};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -5px 0;
    background-color: {theme.accent_primary};
    border-radius: 7px;
}}

QSpinBox, QDoubleSpinBox {{
    background-color: {theme.bg_card};
    color: {theme.text_primary};
    border: 1px solid {theme.border_default};
    border-radius: 4px;
    padding: 4px 8px;
}}

QGroupBox {{
    color: {theme.text_primary};
    border: 1px solid {theme.border_default};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 8px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
}}

QDialog {{
    background-color: {theme.bg_secondary};
}}

QMessageBox {{
    background-color: {theme.bg_secondary};
}}
"""
