"""Settings dialog for EdgePowerMeter."""

from __future__ import annotations
from typing import Optional
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QCheckBox,
    QSpinBox, QDoubleSpinBox, QGroupBox, QWidget,
    QTabWidget, QFrame
)
from PySide6.QtCore import Qt, Signal

from ...core import AppSettings
from ..theme import ThemeColors


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
        self.setMinimumSize(550, 650)
        self.resize(600, 700)
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
        grid.addWidget(QLabel("Max visible points:"), 0, 0)
        self.plot_points_spin = QSpinBox()
        self.plot_points_spin.setRange(500, 50000)
        self.plot_points_spin.setSingleStep(500)
        self.plot_points_spin.setToolTip("Maximum points rendered in the visible window")
        grid.addWidget(self.plot_points_spin, 0, 1)
        
        # Show grid
        self.show_grid_check = QCheckBox("Show grid lines")
        self.show_grid_check.stateChanged.connect(self._on_grid_changed)
        grid.addWidget(self.show_grid_check, 1, 0)
        
        # Grid opacity
        grid.addWidget(QLabel("Grid opacity:"), 2, 0)
        self.grid_alpha_spin = QDoubleSpinBox()
        self.grid_alpha_spin.setRange(0.1, 1.0)
        self.grid_alpha_spin.setSingleStep(0.1)
        self.grid_alpha_spin.setDecimals(1)
        self.grid_alpha_spin.setToolTip("Grid line opacity (0.1 - 1.0)")
        grid.addWidget(self.grid_alpha_spin, 2, 1)
        
        # Crosshair (show values on hover)
        self.show_crosshair_check = QCheckBox("Show cursor values on hover")
        self.show_crosshair_check.setToolTip("Display V/I/P values when hovering over the graph")
        grid.addWidget(self.show_crosshair_check, 3, 0, 1, 2)
        
        # Info label about event-driven updates
        info_label = QLabel("📊 Plot updates automatically when new data arrives (up to 60 FPS)")
        info_label.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        info_label.setWordWrap(True)
        grid.addWidget(info_label, 4, 0, 1, 2)
        
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

        # System monitor group
        sys_group = QGroupBox("System Monitor")
        sys_layout = QVBoxLayout(sys_group)
        sys_layout.setSpacing(8)

        self.show_cpu_usage_check = QCheckBox("Show CPU usage in status bar")
        self.show_cpu_usage_check.setToolTip("Display a live CPU usage bar near the connection status")
        sys_layout.addWidget(self.show_cpu_usage_check)

        sys_info = QLabel("ℹ️ Optional. Uses /proc/stat or psutil if available. Updates ~1 Hz.")
        sys_info.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        sys_info.setWordWrap(True)
        sys_layout.addWidget(sys_info)

        layout.addWidget(sys_group)
        
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
    
    def _on_grid_changed(self, state: int) -> None:
        """Update UI when grid checkbox changes."""
        enabled = state == Qt.Checked
        self.grid_alpha_spin.setEnabled(enabled)
    
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
        self.auto_reconnect_check.setToolTip("Automatically reconnect if the serial port disconnects")
        self.auto_reconnect_check.stateChanged.connect(self._on_reconnect_changed)
        grid.addWidget(self.auto_reconnect_check, 1, 0, 1, 2)
        
        # Reconnect interval
        grid.addWidget(QLabel("Reconnect interval:"), 2, 0)
        self.reconnect_interval_spin = QSpinBox()
        self.reconnect_interval_spin.setRange(1, 30)
        self.reconnect_interval_spin.setSuffix(" s")
        self.reconnect_interval_spin.setToolTip("Seconds between reconnection attempts")
        grid.addWidget(self.reconnect_interval_spin, 2, 1)
        
        # Info about reconnection
        reconnect_info = QLabel("ℹ️ Uses OS events to detect port changes (efficient, no polling)")
        reconnect_info.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        reconnect_info.setWordWrap(True)
        grid.addWidget(reconnect_info, 3, 0, 1, 2)
        
        layout.addWidget(serial_group)
        
        # Sampling group
        sampling_group = QGroupBox("Sampling Rate Control")
        sample_grid = QGridLayout(sampling_group)
        sample_grid.setSpacing(12)
        
        # Target sample rate
        sample_grid.addWidget(QLabel("Target sample rate:"), 0, 0)
        self.target_sample_rate_spin = QSpinBox()
        self.target_sample_rate_spin.setRange(0, 1000)
        self.target_sample_rate_spin.setSuffix(" Hz")
        self.target_sample_rate_spin.setSpecialValueText("Maximum (no limit)")
        self.target_sample_rate_spin.setToolTip(
            "Target sampling rate in Hz.\n"
            "• 0 = Maximum device rate (no subsampling)\n"
            "• < Device max = Subsample to this rate\n"
            "• > Device max = Use maximum device rate"
        )
        sample_grid.addWidget(self.target_sample_rate_spin, 0, 1)
        
        # Device max info
        sample_info = QLabel(
            "ℹ️ Device maximum: ~360 Hz (ESP32+C3 @1MHz I²C, INA226)\n"
            "Set to 0 for maximum throughput, or lower to reduce data volume"
        )
        sample_info.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        sample_info.setWordWrap(True)
        sample_grid.addWidget(sample_info, 1, 0, 1, 2)
        
        layout.addWidget(sampling_group)
        layout.addStretch()
        return widget
    
    def _on_reconnect_changed(self, state: int) -> None:
        """Update UI when auto-reconnect checkbox changes."""
        enabled = state == Qt.Checked
        self.reconnect_interval_spin.setEnabled(enabled)
    
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
        
        # PDF Analysis group
        pdf_group = QGroupBox("PDF Report Analysis")
        pdf_layout = QVBoxLayout(pdf_group)
        pdf_layout.setSpacing(12)
        
        # FFT Analysis
        self.include_fft_check = QCheckBox("Include FFT spectrum analysis")
        self.include_fft_check.setToolTip(
            "Add frequency spectrum analysis of current signal to identify\n"
            "switching noise, ripple, and periodic patterns"
        )
        pdf_layout.addWidget(self.include_fft_check)
        
        fft_info = QLabel(
            "📊 FFT analysis shows frequency components in the current signal.\n"
            "Useful for identifying: switching frequency, PWM ripple, 50/60Hz noise."
        )
        fft_info.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        fft_info.setWordWrap(True)
        pdf_layout.addWidget(fft_info)
        
        # Harmonic Analysis
        pdf_layout.addSpacing(8)
        self.include_harmonic_check = QCheckBox("Include harmonic analysis (THD + spectrum)")
        self.include_harmonic_check.setToolTip(
            "Add Total Harmonic Distortion analysis and individual harmonic components.\n"
            "Essential for power quality assessment and compliance testing."
        )
        pdf_layout.addWidget(self.include_harmonic_check)
        
        # Harmonic settings sub-group
        harmonic_settings = QWidget()
        harmonic_grid = QGridLayout(harmonic_settings)
        harmonic_grid.setContentsMargins(20, 0, 0, 0)
        
        harmonic_grid.addWidget(QLabel("Signal to analyze:"), 0, 0)
        self.harmonic_signal_combo = QComboBox()
        self.harmonic_signal_combo.addItems(["Current", "Voltage", "Power"])
        self.harmonic_signal_combo.setToolTip("Select which signal to analyze for harmonics")
        harmonic_grid.addWidget(self.harmonic_signal_combo, 0, 1)
        
        harmonic_grid.addWidget(QLabel("Max harmonic order:"), 1, 0)
        self.harmonic_max_order_spin = QSpinBox()
        self.harmonic_max_order_spin.setRange(3, 20)
        self.harmonic_max_order_spin.setValue(10)
        self.harmonic_max_order_spin.setToolTip("Maximum harmonic order to analyze (1=fundamental)")
        harmonic_grid.addWidget(self.harmonic_max_order_spin, 1, 1)
        
        pdf_layout.addWidget(harmonic_settings)
        
        # Enable/disable harmonic settings based on checkbox
        def toggle_harmonic_settings(checked: bool):
            harmonic_settings.setEnabled(checked)
        self.include_harmonic_check.toggled.connect(toggle_harmonic_settings)
        harmonic_settings.setEnabled(False)
        
        harmonic_info = QLabel(
            "🔬 Harmonic analysis calculates THD%, individual harmonics (2nd-Nth),\n"
            "and checks compliance with IEC 61000-3-2 limits.\n"
            "Useful for: power supplies, inverters, motor drives, non-linear loads."
        )
        harmonic_info.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        harmonic_info.setWordWrap(True)
        pdf_layout.addWidget(harmonic_info)
        
        layout.addWidget(pdf_group)
        
        layout.addStretch()
        return widget
    
    def _create_about_tab(self) -> QWidget:
        """Create about/info tab with software information."""
        from ...version import __version__, APP_NAME, AUTHOR, DESCRIPTION, URL, LICENSE
        
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
        self.show_grid_check.setChecked(self.settings.show_grid)
        self.show_cpu_usage_check.setChecked(self.settings.show_cpu_usage)
        
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
        self.reconnect_interval_spin.setValue(self.settings.reconnect_interval)
        
        # Sampling
        self.target_sample_rate_spin.setValue(self.settings.target_sample_rate)
        
        # Export - map separator back to display text
        sep_map = {",": ", (comma)", ";": "; (semicolon)", "\t": "\\t (tab)"}
        sep_text = sep_map.get(self.settings.csv_separator, ", (comma)")
        idx = self.csv_separator_combo.findText(sep_text)
        if idx >= 0:
            self.csv_separator_combo.setCurrentIndex(idx)
        
        # Display - grid and crosshair
        self.grid_alpha_spin.setValue(self.settings.grid_alpha)
        self.grid_alpha_spin.setEnabled(self.settings.show_grid)
        self.show_crosshair_check.setChecked(self.settings.show_crosshair)
        
        # Serial - reconnect interval
        self.reconnect_interval_spin.setValue(self.settings.reconnect_interval)
        self.reconnect_interval_spin.setEnabled(self.settings.auto_reconnect)
        
        # Export - FFT
        self.include_fft_check.setChecked(self.settings.include_fft)
        
        # Export - Harmonic Analysis
        self.include_harmonic_check.setChecked(self.settings.include_harmonic_analysis)
        self.harmonic_max_order_spin.setValue(self.settings.harmonic_max_order)
        
        # Map signal type to combo index
        signal_map = {"current": 0, "voltage": 1, "power": 2}
        signal_idx = signal_map.get(self.settings.harmonic_signal.lower(), 0)
        self.harmonic_signal_combo.setCurrentIndex(signal_idx)
    
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
        self.settings.show_grid = self.show_grid_check.isChecked()
        self.settings.grid_alpha = self.grid_alpha_spin.value()
        self.settings.show_crosshair = self.show_crosshair_check.isChecked()
        self.settings.show_cpu_usage = self.show_cpu_usage_check.isChecked()
        
        # Moving average
        self.settings.use_moving_average = self.moving_avg_check.isChecked()
        self.settings.moving_average_window = self.moving_avg_spin.value()
        
        # Serial
        self.settings.baud_rate = int(self.baud_rate_combo.currentText())
        self.settings.auto_reconnect = self.auto_reconnect_check.isChecked()
        self.settings.reconnect_interval = self.reconnect_interval_spin.value()
        
        # Sampling
        self.settings.target_sample_rate = self.target_sample_rate_spin.value()
        
        # Export
        sep_text = self.csv_separator_combo.currentText()
        sep_map = {", (comma)": ",", "; (semicolon)": ";", "\\t (tab)": "\t"}
        self.settings.csv_separator = sep_map.get(sep_text, ",")
        self.settings.timestamp_format = self.timestamp_format_combo.currentText()
        self.settings.include_fft = self.include_fft_check.isChecked()
        
        # Harmonic Analysis
        self.settings.include_harmonic_analysis = self.include_harmonic_check.isChecked()
        self.settings.harmonic_max_order = self.harmonic_max_order_spin.value()
        
        # Map combo index to signal type
        signal_types = ["current", "voltage", "power"]
        self.settings.harmonic_signal = signal_types[self.harmonic_signal_combo.currentIndex()]
        
        # Emit signals
        self.settings_changed.emit(self.settings)
        
        if self.settings.dark_mode != old_dark_mode:
            self.theme_changed.emit(self.settings.dark_mode)
        
        self.accept()
