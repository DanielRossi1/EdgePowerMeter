"""Settings dialog for EdgePowerMeter."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Callable
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QCheckBox,
    QSpinBox, QDoubleSpinBox, QGroupBox, QWidget,
    QTabWidget, QFrame
)
from PySide6.QtCore import Qt, Signal

from .theme import ThemeColors, DARK_THEME, LIGHT_THEME, generate_stylesheet


@dataclass
class AppSettings:
    """Application settings."""
    # Theme
    dark_mode: bool = True
    
    # Units
    voltage_unit: str = "V"
    current_unit: str = "A"
    power_unit: str = "W"
    
    # Display
    plot_points: int = 1000
    update_interval_ms: int = 100
    show_grid: bool = True
    antialiasing: bool = True
    
    # Average Power
    use_moving_average: bool = True
    moving_average_window: int = 100
    
    # Serial
    baud_rate: int = 921600  # High-speed for ESP32-C3
    auto_reconnect: bool = True
    
    # Export
    csv_separator: str = ","
    timestamp_format: str = "%Y-%m-%d %H:%M:%S.%f"


class SettingsDialog(QDialog):
    """Settings dialog with modern styling."""
    
    settings_changed = Signal(AppSettings)
    theme_changed = Signal(bool)  # True = dark mode
    
    def __init__(
        self,
        settings: AppSettings,
        theme: ThemeColors,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.settings = settings
        self.theme = theme
        self._setup_ui()
        self._apply_theme()
        self._load_settings()
    
    def _apply_theme(self) -> None:
        """Apply theme styling to dialog."""
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {self.theme.bg_secondary};
            }}
            QLabel {{
                color: {self.theme.text_primary};
            }}
            QGroupBox {{
                color: {self.theme.text_primary};
                border: 1px solid {self.theme.border_default};
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 8px;
                font-weight: 500;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }}
            QTabWidget::pane {{
                border: 1px solid {self.theme.border_default};
                border-radius: 6px;
                background-color: {self.theme.bg_card};
            }}
            QTabBar::tab {{
                background-color: {self.theme.bg_secondary};
                color: {self.theme.text_secondary};
                border: 1px solid {self.theme.border_default};
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                padding: 8px 16px;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background-color: {self.theme.bg_card};
                color: {self.theme.text_primary};
            }}
            QComboBox {{
                background-color: {self.theme.bg_card};
                color: {self.theme.text_primary};
                border: 1px solid {self.theme.border_default};
                border-radius: 4px;
                padding: 6px 10px;
            }}
            QComboBox::drop-down {{
                border: none;
            }}
            QComboBox QAbstractItemView {{
                background-color: {self.theme.bg_card};
                color: {self.theme.text_primary};
                selection-background-color: {self.theme.accent_primary};
            }}
            QSpinBox {{
                background-color: {self.theme.bg_card};
                color: {self.theme.text_primary};
                border: 1px solid {self.theme.border_default};
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QCheckBox {{
                color: {self.theme.text_primary};
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 3px;
                border: 1px solid {self.theme.border_default};
                background-color: {self.theme.bg_card};
            }}
            QCheckBox::indicator:checked {{
                background-color: {self.theme.accent_primary};
                border-color: {self.theme.accent_primary};
            }}
            QPushButton {{
                background-color: {self.theme.bg_card};
                color: {self.theme.text_primary};
                border: 1px solid {self.theme.border_default};
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {self.theme.bg_elevated};
                border-color: {self.theme.border_hover};
            }}
            QPushButton[class="primary"] {{
                background-color: {self.theme.accent_primary};
                border-color: {self.theme.accent_primary};
                color: white;
            }}
            QFrame[class="card"] {{
                background-color: {self.theme.bg_card};
                border: 1px solid {self.theme.border_default};
                border-radius: 6px;
            }}
        """)
    
    def _setup_ui(self) -> None:
        """Set up the dialog UI."""
        self.setWindowTitle("Settings")
        self.setMinimumSize(450, 500)
        self.setModal(True)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        # Title
        title = QLabel("⚙️ Settings")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)
        
        # Tab widget
        tabs = QTabWidget()
        tabs.addTab(self._create_appearance_tab(), "🎨 Appearance")
        tabs.addTab(self._create_units_tab(), "📏 Units")
        tabs.addTab(self._create_display_tab(), "📊 Display")
        tabs.addTab(self._create_serial_tab(), "🔌 Serial")
        tabs.addTab(self._create_export_tab(), "📁 Export")
        tabs.addTab(self._create_about_tab(), "ℹ️ About")
        layout.addWidget(tabs)
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumWidth(100)
        cancel_btn.clicked.connect(self.reject)
        
        apply_btn = QPushButton("Apply")
        apply_btn.setProperty("class", "primary")
        apply_btn.setMinimumWidth(100)
        apply_btn.clicked.connect(self._apply_settings)
        
        button_layout.addWidget(cancel_btn)
        button_layout.addWidget(apply_btn)
        layout.addLayout(button_layout)
    
    def _create_appearance_tab(self) -> QWidget:
        """Create appearance settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(16)
        
        # Theme group
        theme_group = QGroupBox("Theme")
        theme_layout = QVBoxLayout(theme_group)
        
        self.dark_mode_check = QCheckBox("Dark Mode")
        self.dark_mode_check.setToolTip("Enable dark theme for the application")
        theme_layout.addWidget(self.dark_mode_check)
        
        # Theme preview
        preview_frame = QFrame()
        preview_frame.setProperty("class", "card")
        preview_frame.setFixedHeight(60)
        preview_layout = QHBoxLayout(preview_frame)
        
        preview_label = QLabel("Theme Preview")
        preview_label.setStyleSheet("color: #8b949e;")
        preview_layout.addWidget(preview_label)
        
        theme_layout.addWidget(preview_frame)
        layout.addWidget(theme_group)
        layout.addStretch()
        
        return widget
    
    def _create_units_tab(self) -> QWidget:
        """Create units settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(16)
        
        # Units group
        units_group = QGroupBox("Measurement Units")
        grid = QGridLayout(units_group)
        grid.setSpacing(12)
        
        # Voltage
        grid.addWidget(QLabel("Voltage:"), 0, 0)
        self.voltage_unit_combo = QComboBox()
        self.voltage_unit_combo.addItems(["V", "mV"])
        grid.addWidget(self.voltage_unit_combo, 0, 1)
        
        # Current
        grid.addWidget(QLabel("Current:"), 1, 0)
        self.current_unit_combo = QComboBox()
        self.current_unit_combo.addItems(["A", "mA", "µA"])
        grid.addWidget(self.current_unit_combo, 1, 1)
        
        # Power
        grid.addWidget(QLabel("Power:"), 2, 0)
        self.power_unit_combo = QComboBox()
        self.power_unit_combo.addItems(["W", "mW", "µW"])
        grid.addWidget(self.power_unit_combo, 2, 1)
        
        layout.addWidget(units_group)
        
        # Info
        info = QLabel("ℹ️ Unit conversion will be applied to displayed values and exports.")
        info.setStyleSheet("color: #8b949e; font-size: 11px;")
        info.setWordWrap(True)
        layout.addWidget(info)
        
        layout.addStretch()
        return widget
    
    def _create_display_tab(self) -> QWidget:
        """Create display settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(16)
        
        # Plot group
        plot_group = QGroupBox("Plot Settings")
        grid = QGridLayout(plot_group)
        grid.setSpacing(12)
        
        # Plot points
        grid.addWidget(QLabel("Max plot points:"), 0, 0)
        self.plot_points_spin = QSpinBox()
        self.plot_points_spin.setRange(100, 10000)
        self.plot_points_spin.setSingleStep(100)
        self.plot_points_spin.setToolTip("Maximum number of points shown on plots")
        grid.addWidget(self.plot_points_spin, 0, 1)
        
        # Update interval
        grid.addWidget(QLabel("Update interval (ms):"), 1, 0)
        self.update_interval_spin = QSpinBox()
        self.update_interval_spin.setRange(50, 1000)
        self.update_interval_spin.setSingleStep(50)
        self.update_interval_spin.setToolTip("Plot refresh rate in milliseconds")
        grid.addWidget(self.update_interval_spin, 1, 1)
        
        # Show grid
        self.show_grid_check = QCheckBox("Show grid lines")
        grid.addWidget(self.show_grid_check, 2, 0, 1, 2)
        
        # Antialiasing
        self.antialiasing_check = QCheckBox("Enable antialiasing")
        self.antialiasing_check.setToolTip("Smoother lines but may impact performance")
        grid.addWidget(self.antialiasing_check, 3, 0, 1, 2)
        
        layout.addWidget(plot_group)
        
        # Average Power group
        avg_group = QGroupBox("Average Power")
        avg_layout = QGridLayout(avg_group)
        avg_layout.setSpacing(12)
        
        # Moving average checkbox
        self.moving_avg_check = QCheckBox("Use moving average")
        self.moving_avg_check.setToolTip("Use a sliding window average instead of total average")
        self.moving_avg_check.stateChanged.connect(self._on_moving_avg_changed)
        avg_layout.addWidget(self.moving_avg_check, 0, 0, 1, 2)
        
        # Window size
        avg_layout.addWidget(QLabel("Window size:"), 1, 0)
        self.moving_avg_spin = QSpinBox()
        self.moving_avg_spin.setRange(10, 1000)
        self.moving_avg_spin.setSingleStep(10)
        self.moving_avg_spin.setToolTip("Number of samples for moving average")
        avg_layout.addWidget(self.moving_avg_spin, 1, 1)
        
        # Info label
        self.avg_info_label = QLabel()
        self.avg_info_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        self.avg_info_label.setWordWrap(True)
        avg_layout.addWidget(self.avg_info_label, 2, 0, 1, 2)
        
        layout.addWidget(avg_group)
        
        layout.addStretch()
        return widget
    
    def _on_moving_avg_changed(self, state: int) -> None:
        """Update UI when moving average checkbox changes."""
        enabled = state == Qt.Checked
        self.moving_avg_spin.setEnabled(enabled)
        if enabled:
            self.avg_info_label.setText(f"Average calculated over last {self.moving_avg_spin.value()} samples.")
        else:
            self.avg_info_label.setText("Average calculated over all recorded samples.")
    
    def _create_serial_tab(self) -> QWidget:
        """Create serial settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(16)
        
        # Serial group
        serial_group = QGroupBox("Serial Connection")
        grid = QGridLayout(serial_group)
        grid.setSpacing(12)
        
        # Baud rate
        grid.addWidget(QLabel("Baud rate:"), 0, 0)
        self.baud_rate_combo = QComboBox()
        self.baud_rate_combo.addItems(["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"])
        grid.addWidget(self.baud_rate_combo, 0, 1)
        
        # Auto reconnect
        self.auto_reconnect_check = QCheckBox("Auto-reconnect on disconnect")
        grid.addWidget(self.auto_reconnect_check, 1, 0, 1, 2)
        
        layout.addWidget(serial_group)
        layout.addStretch()
        return widget
    
    def _create_export_tab(self) -> QWidget:
        """Create export settings tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(16)
        
        # CSV group
        csv_group = QGroupBox("CSV Export")
        grid = QGridLayout(csv_group)
        grid.setSpacing(12)
        
        # Separator
        grid.addWidget(QLabel("Field separator:"), 0, 0)
        self.csv_separator_combo = QComboBox()
        self.csv_separator_combo.addItems([", (comma)", "; (semicolon)", "\\t (tab)"])
        grid.addWidget(self.csv_separator_combo, 0, 1)
        
        # Timestamp format
        grid.addWidget(QLabel("Timestamp format:"), 1, 0)
        self.timestamp_format_combo = QComboBox()
        self.timestamp_format_combo.addItems([
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "Unix timestamp"
        ])
        grid.addWidget(self.timestamp_format_combo, 1, 1)
        
        layout.addWidget(csv_group)
        layout.addStretch()
        return widget
    
    def _create_about_tab(self) -> QWidget:
        """Create about/info tab with software information."""
        from ..version import __version__, APP_NAME, AUTHOR, DESCRIPTION, URL, LICENSE
        
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(16)
        
        # App info group
        app_group = QGroupBox("Application")
        app_layout = QVBoxLayout(app_group)
        app_layout.setSpacing(8)
        
        # App name and version
        name_label = QLabel(f"⚡ {APP_NAME}")
        name_label.setStyleSheet("font-size: 18px; font-weight: 700;")
        app_layout.addWidget(name_label)
        
        version_label = QLabel(f"Version {__version__}")
        version_label.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 14px;")
        app_layout.addWidget(version_label)
        
        desc_label = QLabel(DESCRIPTION)
        desc_label.setStyleSheet(f"color: {self.theme.text_secondary};")
        desc_label.setWordWrap(True)
        app_layout.addWidget(desc_label)
        
        layout.addWidget(app_group)
        
        # Author group
        author_group = QGroupBox("Author")
        author_layout = QVBoxLayout(author_group)
        author_layout.setSpacing(8)
        
        author_label = QLabel(f"👤 {AUTHOR}")
        author_layout.addWidget(author_label)
        
        url_label = QLabel(f"🔗 <a href='{URL}' style='color: {self.theme.accent_primary};'>{URL}</a>")
        url_label.setOpenExternalLinks(True)
        author_layout.addWidget(url_label)
        
        layout.addWidget(author_group)
        
        # License group
        license_group = QGroupBox("License")
        license_layout = QVBoxLayout(license_group)
        
        license_label = QLabel(f"📄 {LICENSE}")
        license_layout.addWidget(license_label)
        
        license_info = QLabel("This software is open source. See LICENSE file for details.")
        license_info.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        license_info.setWordWrap(True)
        license_layout.addWidget(license_info)
        
        layout.addWidget(license_group)
        
        # System info
        import sys
        import platform
        
        sys_group = QGroupBox("System")
        sys_layout = QGridLayout(sys_group)
        sys_layout.setSpacing(8)
        
        sys_layout.addWidget(QLabel("Python:"), 0, 0)
        sys_layout.addWidget(QLabel(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"), 0, 1)
        
        sys_layout.addWidget(QLabel("Platform:"), 1, 0)
        sys_layout.addWidget(QLabel(platform.system()), 1, 1)
        
        try:
            from PySide6 import __version__ as pyside_version
            sys_layout.addWidget(QLabel("PySide6:"), 2, 0)
            sys_layout.addWidget(QLabel(pyside_version), 2, 1)
        except ImportError:
            pass
        
        layout.addWidget(sys_group)
        
        layout.addStretch()
        return widget
    
    def _load_settings(self) -> None:
        """Load current settings into UI."""
        self.dark_mode_check.setChecked(self.settings.dark_mode)
        
        # Units
        idx = self.voltage_unit_combo.findText(self.settings.voltage_unit)
        if idx >= 0:
            self.voltage_unit_combo.setCurrentIndex(idx)
        
        idx = self.current_unit_combo.findText(self.settings.current_unit)
        if idx >= 0:
            self.current_unit_combo.setCurrentIndex(idx)
        
        idx = self.power_unit_combo.findText(self.settings.power_unit)
        if idx >= 0:
            self.power_unit_combo.setCurrentIndex(idx)
        
        # Display
        self.plot_points_spin.setValue(self.settings.plot_points)
        self.update_interval_spin.setValue(self.settings.update_interval_ms)
        self.show_grid_check.setChecked(self.settings.show_grid)
        self.antialiasing_check.setChecked(self.settings.antialiasing)
        
        # Moving average
        self.moving_avg_check.setChecked(self.settings.use_moving_average)
        self.moving_avg_spin.setValue(self.settings.moving_average_window)
        self.moving_avg_spin.setEnabled(self.settings.use_moving_average)
        self._on_moving_avg_changed(Qt.Checked if self.settings.use_moving_average else Qt.Unchecked)
        
        # Serial
        idx = self.baud_rate_combo.findText(str(self.settings.baud_rate))
        if idx >= 0:
            self.baud_rate_combo.setCurrentIndex(idx)
        self.auto_reconnect_check.setChecked(self.settings.auto_reconnect)
        
        # Export - map separator back to display text
        sep_map = {",": ", (comma)", ";": "; (semicolon)", "\t": "\\t (tab)"}
        sep_text = sep_map.get(self.settings.csv_separator, ", (comma)")
        idx = self.csv_separator_combo.findText(sep_text)
        if idx >= 0:
            self.csv_separator_combo.setCurrentIndex(idx)
    
    def _apply_settings(self) -> None:
        """Apply settings and close dialog."""
        old_dark_mode = self.settings.dark_mode
        
        # Theme
        self.settings.dark_mode = self.dark_mode_check.isChecked()
        
        # Units
        self.settings.voltage_unit = self.voltage_unit_combo.currentText()
        self.settings.current_unit = self.current_unit_combo.currentText()
        self.settings.power_unit = self.power_unit_combo.currentText()
        
        # Display
        self.settings.plot_points = self.plot_points_spin.value()
        self.settings.update_interval_ms = self.update_interval_spin.value()
        self.settings.show_grid = self.show_grid_check.isChecked()
        self.settings.antialiasing = self.antialiasing_check.isChecked()
        
        # Moving average
        self.settings.use_moving_average = self.moving_avg_check.isChecked()
        self.settings.moving_average_window = self.moving_avg_spin.value()
        
        # Serial
        self.settings.baud_rate = int(self.baud_rate_combo.currentText())
        self.settings.auto_reconnect = self.auto_reconnect_check.isChecked()
        
        # Export
        sep_text = self.csv_separator_combo.currentText()
        sep_map = {", (comma)": ",", "; (semicolon)": ";", "\\t (tab)": "\t"}
        self.settings.csv_separator = sep_map.get(sep_text, ",")
        self.settings.timestamp_format = self.timestamp_format_combo.currentText()
        
        # Emit signals
        self.settings_changed.emit(self.settings)
        
        if self.settings.dark_mode != old_dark_mode:
            self.theme_changed.emit(self.settings.dark_mode)
        
        self.accept()
