"""Theme palettes and the application stylesheet."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThemeColors:
    name: str
    bg: str             # window background
    surface: str        # cards, panels
    surface_alt: str    # inputs, hovered rows
    border: str
    text: str
    text_secondary: str
    text_muted: str
    accent: str
    accent_text: str    # text on accent background
    success: str
    warning: str
    danger: str
    chart_voltage: str
    chart_current: str
    chart_power: str
    plot_bg: str
    grid: str

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


DARK_THEME = ThemeColors(
    name="dark",
    bg="#0e1116",
    surface="#151a21",
    surface_alt="#1d232c",
    border="#262e39",
    text="#e6e9ef",
    text_secondary="#9aa4b2",
    text_muted="#5f6977",
    accent="#4c8dff",
    accent_text="#ffffff",
    success="#2fbf71",
    warning="#e0a63a",
    danger="#ef5b5b",
    chart_voltage="#4c8dff",
    chart_current="#f0a33a",
    chart_power="#2fbf71",
    plot_bg="#11151b",
    grid="#2a323d",
)

LIGHT_THEME = ThemeColors(
    name="light",
    bg="#f3f5f8",
    surface="#ffffff",
    surface_alt="#eef1f5",
    border="#dde2e9",
    text="#18202b",
    text_secondary="#556070",
    text_muted="#9aa3b0",
    accent="#2f6fec",
    accent_text="#ffffff",
    success="#1f9d5b",
    warning="#c7811c",
    danger="#d64545",
    chart_voltage="#2f6fec",
    chart_current="#d9822b",
    chart_power="#1f9d5b",
    plot_bg="#ffffff",
    grid="#e6e9ee",
)


_system_window_lightness = None   # captured before the app applies its own palette


def remember_system_palette() -> None:
    """Snapshot the desktop palette once, before apply_theme() replaces it;
    afterwards QGuiApplication.palette() only reflects our own theme."""
    global _system_window_lightness
    if _system_window_lightness is None:
        try:
            from PySide6 import QtGui
            _system_window_lightness = QtGui.QGuiApplication.palette().window().color().lightness()
        except Exception:
            _system_window_lightness = 0


def resolve_theme(name: str) -> ThemeColors:
    """'dark' | 'light' | 'system' -> palette."""
    remember_system_palette()
    if name == "system":
        try:
            from PySide6 import QtCore, QtGui
            scheme = QtGui.QGuiApplication.styleHints().colorScheme()
            if scheme == QtCore.Qt.ColorScheme.Light:
                return LIGHT_THEME
            if scheme == QtCore.Qt.ColorScheme.Dark:
                return DARK_THEME
        except Exception:
            pass
        return DARK_THEME if (_system_window_lightness or 0) < 128 else LIGHT_THEME
    return LIGHT_THEME if name == "light" else DARK_THEME


def build_palette(t: ThemeColors):
    """QPalette matching the theme.

    The stylesheet does not cover everything Qt paints (dialog backgrounds,
    popup frames, file-dialog views, selection colors), and those parts fall
    back to the palette; without this, dialogs kept the system's light
    background under the dark theme.
    """
    from PySide6.QtGui import QColor, QPalette

    p = QPalette()
    roles = {
        QPalette.Window: t.bg,
        QPalette.WindowText: t.text,
        QPalette.Base: t.surface_alt,
        QPalette.AlternateBase: t.surface,
        QPalette.ToolTipBase: t.surface_alt,
        QPalette.ToolTipText: t.text,
        QPalette.PlaceholderText: t.text_muted,
        QPalette.Text: t.text,
        QPalette.Button: t.surface_alt,
        QPalette.ButtonText: t.text,
        QPalette.BrightText: t.danger,
        QPalette.Highlight: t.accent,
        QPalette.HighlightedText: t.accent_text,
        QPalette.Link: t.accent,
        QPalette.Light: t.border,
        QPalette.Midlight: t.border,
        QPalette.Mid: t.border,
        QPalette.Dark: t.bg,
        QPalette.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(t.text_muted))
    return p


def apply_theme(app, t: ThemeColors) -> None:
    """Apply palette + stylesheet application-wide, so every top-level window
    (message boxes, progress and file dialogs, combo popups, tooltips)
    follows the theme, not just the main window."""
    remember_system_palette()
    app.setPalette(build_palette(t))
    app.setStyleSheet(generate_stylesheet(t))


def generate_stylesheet(t: ThemeColors) -> str:
    return _STYLESHEET_TEMPLATE(t).replace("__CHECK_ICON__", _check_icon_path())


def _check_icon_path() -> str:
    """Write the checkbox tick as a tiny SVG file (Qt stylesheets need a URL)."""
    import os
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "edgepowermeter-check.svg")
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="ascii") as f:
                f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                        '<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="#ffffff" '
                        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
        except OSError:
            return ""
    return path.replace("\\", "/")


def _STYLESHEET_TEMPLATE(t: ThemeColors) -> str:
    return f"""
* {{
    font-size: 13px;
}}
/* Generic rule first: rules below with the same specificity must override it
   (later rules win), otherwise top-level dialogs end up transparent. */
QWidget {{
    color: {t.text};
    background: transparent;
}}
QMainWindow, QWidget#Root, QDialog, QMessageBox, QProgressDialog, QFileDialog {{
    background: {t.bg};
}}
QListView, QTreeView {{
    background: {t.surface};
    alternate-background-color: {t.surface_alt};
    border: 1px solid {t.border};
    border-radius: 6px;
    selection-background-color: {t.accent};
    selection-color: {t.accent_text};
}}
QMenu {{
    background: {t.surface};
    border: 1px solid {t.border};
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 18px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background: {t.surface_alt};
}}
QToolTip {{
    background: {t.surface_alt};
    color: {t.text};
    border: 1px solid {t.border};
    padding: 4px 6px;
}}

/* ---- navigation rail ---- */
QFrame#NavRail {{
    background: {t.surface};
    border-right: 1px solid {t.border};
}}
QToolButton#NavButton {{
    border: none;
    border-radius: 10px;
    padding: 8px 2px 6px 2px;
    color: {t.text_secondary};
    font-size: 11px;
}}
QToolButton#NavButton:hover {{
    background: {t.surface_alt};
    color: {t.text};
}}
QToolButton#NavButton:checked {{
    background: {t.surface_alt};
    color: {t.accent};
    font-weight: 600;
}}
/* ---- top bar ---- */
QFrame#TopBar {{
    background: {t.surface};
    border-bottom: 1px solid {t.border};
}}
QLabel#PageTitle {{
    font-size: 17px;
    font-weight: 600;
}}

/* ---- cards ---- */
QFrame#Card {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: 12px;
}}
QLabel#CardTitle {{
    font-size: 14px;
    font-weight: 600;
}}
QLabel#Hint, QLabel#Muted {{
    color: {t.text_secondary};
    font-size: 12px;
}}
QLabel#MetricName {{
    color: {t.text_secondary};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}
QLabel#MetricValue {{
    font-size: 26px;
    font-weight: 600;
    font-family: "JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace;
}}
QLabel#MetricUnit {{
    color: {t.text_secondary};
    font-size: 14px;
}}
QLabel#KeyLabel {{
    color: {t.text_secondary};
}}
QLabel#KeyValue {{
    font-family: "JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace;
}}

/* ---- buttons ---- */
QPushButton, QToolButton#Chip {{
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-radius: 8px;
    padding: 6px 14px;
    font-weight: 500;
}}
QPushButton:hover, QToolButton#Chip:hover {{
    border-color: {t.accent};
}}
QPushButton:pressed {{
    background: {t.border};
}}
QPushButton:disabled {{
    color: {t.text_muted};
    border-color: {t.border};
    background: {t.surface};
}}
QPushButton[variant="primary"] {{
    background: {t.accent};
    border-color: {t.accent};
    color: {t.accent_text};
    font-weight: 600;
}}
QPushButton[variant="success"] {{
    background: {t.success};
    border-color: {t.success};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[variant="danger"] {{
    background: {t.danger};
    border-color: {t.danger};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[variant="primary"]:disabled, QPushButton[variant="success"]:disabled,
QPushButton[variant="danger"]:disabled {{
    background: {t.surface_alt};
    border-color: {t.border};
    color: {t.text_muted};
}}
QPushButton[variant="flat"] {{
    background: transparent;
    border-color: transparent;
}}
QPushButton[variant="flat"]:hover {{
    background: {t.surface_alt};
}}
QToolButton#Chip {{
    padding: 4px 10px;
    border-radius: 13px;
    color: {t.text_secondary};
}}
QToolButton#Chip:checked {{
    color: {t.text};
    border-color: {t.accent};
    background: {t.surface};
}}

/* ---- inputs ---- */
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-radius: 8px;
    padding: 5px 8px;
    selection-background-color: {t.accent};
    selection-color: {t.accent_text};
}}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QLineEdit:hover {{
    border-color: {t.accent};
}}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {t.accent};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background: {t.surface};
    border: 1px solid {t.border};
    selection-background-color: {t.surface_alt};
    selection-color: {t.text};
    outline: none;
}}
/* Qt bug: with a styled border, NoButtons collapses the editor to 1 px, so
   spin boxes keep their buttons and the buttons are made zero-width here. */
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 0px;
    border: none;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 0px;
    border: none;
}}
QAbstractSpinBox QLineEdit {{
    background: transparent;
    border: none;
    padding: 0px 0px 0px 6px;
}}
QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 5px;
    border: 1px solid {t.text_muted};
    background: {t.surface_alt};
}}
QCheckBox::indicator:hover {{
    border-color: {t.accent};
}}
QCheckBox::indicator:checked {{
    background: {t.accent};
    border-color: {t.accent};
    image: url(__CHECK_ICON__);
}}
QRadioButton {{
    spacing: 8px;
}}
QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border-radius: 8px;
    border: 1px solid {t.text_muted};
    background: {t.surface_alt};
}}
QRadioButton::indicator:checked {{
    border: 4px solid {t.accent};
    background: {t.accent_text};
}}
QCheckBox::indicator:disabled {{
    border-color: {t.border};
    background: {t.surface};
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {t.border};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {t.accent};
    width: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}

/* ---- tables / tabs / scroll ---- */
QTableWidget {{
    background: transparent;
    border: none;
    gridline-color: {t.border};
}}
QTableWidget::item {{
    padding: 4px 8px;
}}
QHeaderView::section {{
    background: transparent;
    color: {t.text_secondary};
    border: none;
    border-bottom: 1px solid {t.border};
    padding: 6px 8px;
    font-weight: 600;
}}
QTabWidget::pane {{
    border: none;
}}
QTabBar::tab {{
    background: transparent;
    color: {t.text_secondary};
    padding: 8px 14px;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}
QTabBar::tab:selected {{
    color: {t.text};
    border-bottom-color: {t.accent};
}}
QTabBar::tab:hover {{
    color: {t.text};
}}
QScrollArea {{
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {t.border};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t.text_muted};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    width: 0px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {t.border};
    border-radius: 4px;
    min-width: 30px;
}}
QSplitter::handle {{
    background: transparent;
}}

/* ---- status bar / banners ---- */
QStatusBar {{
    background: {t.surface};
    border-top: 1px solid {t.border};
    color: {t.text_secondary};
}}
QStatusBar QLabel {{
    color: {t.text_secondary};
    padding: 0 6px;
}}
QFrame#Banner {{
    border-radius: 8px;
}}
QProgressBar {{
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-radius: 6px;
    text-align: center;
    height: 12px;
}}
QProgressBar::chunk {{
    background: {t.accent};
    border-radius: 6px;
}}
"""
