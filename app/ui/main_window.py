"""Main window for EdgePowerMeter application.

Clean, modular UI for real-time power monitoring with data analysis
and export capabilities.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Deque

from PySide6 import QtCore, QtWidgets, QtGui

from ..serial import SerialReader
from ..core import AppSettings, Statistics, MeasurementRecord
from ..export import ReportGenerator, CSVImporter
from ..version import __version__, APP_NAME
from .theme import ThemeColors, DARK_THEME, LIGHT_THEME, generate_stylesheet
from .dialogs import SettingsDialog
from .widgets import PlotBuffers, PlotWidget, StatCard, PortDiscovery


# =============================================================================
# Main Window
# =============================================================================

class MainWindow(QtWidgets.QMainWindow):
    """Main application window."""
    
    MIN_PLOT_INTERVAL_MS = 16  # Max 60 FPS (~16ms between updates)
    STOP_TIMEOUT_MS = 3000
    MAX_DATA_POINTS = 100000  # Max points before warning
    
    def __init__(self):
        super().__init__()
        self._init_state()
        self._setup_ui()
        self._apply_loaded_settings()
        self._connect_signals()
        self._setup_plot_throttle()
        self._setup_port_monitor()
        self._refresh_ports()
    
    def _init_state(self) -> None:
        self.reader: Optional[SerialReader] = None
        self.buffers = PlotBuffers()
        self.full_data: List[MeasurementRecord] = []
        self.report_generator = ReportGenerator()
        
        # Load settings from persistent storage
        self.settings = AppSettings.load()
        self.theme = DARK_THEME if self.settings.dark_mode else LIGHT_THEME
        
        # Flag to track if we're actively acquiring
        self._acquiring = False
        self._acq_start_time: float = 0.0  # perf_counter at acquisition start
        
        # Rate limiting for plot updates (max 60 FPS)
        self._last_plot_update: float = 0.0
        
        # Running statistics for efficient avg power calculation
        self._power_sum: float = 0.0
        self._power_window: Deque[float] = deque(maxlen=1000)  # For moving average
        
        # Auto-reconnect state
        self._last_port: Optional[str] = None
        self._reconnect_timer: Optional[QtCore.QTimer] = None
    
    def _setup_ui(self) -> None:
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.setMinimumSize(1200, 800)
        self.resize(1400, 900)
        
        # Set window icon - handle both development and PyInstaller bundle
        import sys
        if getattr(sys, 'frozen', False):
            # Running as PyInstaller bundle
            base_path = Path(sys._MEIPASS)
        else:
            # Running in development
            base_path = Path(__file__).parent.parent.parent
        
        icon_path = base_path / "assets" / "icons" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QtGui.QIcon(str(icon_path)))
        
        self._apply_theme()
        
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)
        
        self._create_header(main_layout)
        self._create_connection_bar(main_layout)
        
        content = QtWidgets.QHBoxLayout()
        content.setSpacing(12)
        
        self.plot_widget = PlotWidget(self.theme)
        content.addWidget(self.plot_widget, stretch=4)
        
        self._create_stats_panel(content)
        main_layout.addLayout(content, stretch=1)
        
        self._create_control_bar(main_layout)
        self._create_status_bar(main_layout)
    
    def _apply_loaded_settings(self) -> None:
        """Apply all loaded settings to UI components after setup."""
        s = self.settings
        
        # Plot widget settings
        self.plot_widget.set_grid(s.show_grid, s.grid_alpha)
        self.plot_widget.set_crosshair(s.show_crosshair)
        
        # Report generator
        self.report_generator.include_fft = s.include_fft
        
        # Power window for moving average
        self._power_window = deque(maxlen=s.moving_average_window)
    
    def _apply_theme(self) -> None:
        self.setStyleSheet(generate_stylesheet(self.theme))
    
    def _create_header(self, parent: QtWidgets.QVBoxLayout) -> None:
        header = QtWidgets.QHBoxLayout()
        
        title = QtWidgets.QLabel("⚡ EdgePowerMeter")
        title.setProperty("class", "title")
        header.addWidget(title)
        
        subtitle = QtWidgets.QLabel("Real-time Power Analysis")
        subtitle.setProperty("class", "subtitle")
        header.addWidget(subtitle)
        
        header.addStretch()
        
        self.settings_btn = QtWidgets.QPushButton("⚙️")
        self.settings_btn.setProperty("class", "icon")
        self.settings_btn.setFixedSize(36, 36)
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        header.addWidget(self.settings_btn)
        
        parent.addLayout(header)
    
    def _create_connection_bar(self, parent: QtWidgets.QVBoxLayout) -> None:
        frame = QtWidgets.QFrame()
        frame.setProperty("class", "card")
        
        layout = QtWidgets.QHBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        
        layout.addWidget(QtWidgets.QLabel("Port:"))
        
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(280)
        layout.addWidget(self.port_combo)
        
        self.refresh_btn = QtWidgets.QPushButton("↻ Refresh")
        self.refresh_btn.setMinimumWidth(100)
        layout.addWidget(self.refresh_btn)
        
        self.show_all_cb = QtWidgets.QCheckBox("Show all")
        layout.addWidget(self.show_all_cb)
        
        layout.addStretch()
        
        # Start and Stop buttons together
        self.connect_btn = QtWidgets.QPushButton("▶ Start")
        self.connect_btn.setProperty("class", "success")
        self.connect_btn.setMinimumWidth(100)
        layout.addWidget(self.connect_btn)
        
        self.stop_btn = QtWidgets.QPushButton("⏹ Stop")
        self.stop_btn.setProperty("class", "danger")
        self.stop_btn.setMinimumWidth(100)
        self.stop_btn.setEnabled(False)
        layout.addWidget(self.stop_btn)
        
        parent.addWidget(frame)
    
    def _create_stats_panel(self, parent: QtWidgets.QHBoxLayout) -> None:
        frame = QtWidgets.QFrame()
        frame.setProperty("class", "card")
        frame.setFixedWidth(200)
        
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        
        title = QtWidgets.QLabel("📊 Live")
        title.setProperty("class", "subtitle")
        layout.addWidget(title)
        
        self.voltage_card = StatCard("VOLTAGE", "V", self.theme.chart_voltage)
        layout.addWidget(self.voltage_card)
        
        self.current_card = StatCard("CURRENT", "A", self.theme.chart_current)
        layout.addWidget(self.current_card)
        
        self.power_card = StatCard("POWER", "W", self.theme.chart_power)
        layout.addWidget(self.power_card)
        
        self.avg_power_card = StatCard("AVG POWER", "W", self.theme.accent_primary)
        layout.addWidget(self.avg_power_card)
        
        layout.addSpacing(10)
        
        sel_title = QtWidgets.QLabel("📐 Selection")
        sel_title.setProperty("class", "subtitle")
        layout.addWidget(sel_title)
        
        self.sel_samples_label = QtWidgets.QLabel("Samples: --")
        self.sel_samples_label.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        layout.addWidget(self.sel_samples_label)
        
        self.sel_duration_label = QtWidgets.QLabel("Duration: --")
        self.sel_duration_label.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        layout.addWidget(self.sel_duration_label)
        
        self.sel_power_label = QtWidgets.QLabel("Avg Power: --")
        self.sel_power_label.setStyleSheet(f"color: {self.theme.chart_power}; font-size: 11px;")
        layout.addWidget(self.sel_power_label)
        
        layout.addStretch()
        parent.addWidget(frame)
    
    def _create_control_bar(self, parent: QtWidgets.QVBoxLayout) -> None:
        frame = QtWidgets.QFrame()
        frame.setProperty("class", "card")
        
        layout = QtWidgets.QHBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        
        self.import_btn = QtWidgets.QPushButton("📂 Import")
        self.import_btn.setMinimumWidth(90)
        self.import_btn.setToolTip("Import data from CSV file")
        layout.addWidget(self.import_btn)
        
        layout.addStretch()
        
        self.select_all_btn = QtWidgets.QPushButton("Select All")
        self.select_all_btn.setMinimumWidth(100)
        self.select_all_btn.setEnabled(False)
        layout.addWidget(self.select_all_btn)
        
        layout.addSpacing(10)
        
        self.export_csv_btn = QtWidgets.QPushButton("📄 CSV")
        self.export_csv_btn.setMinimumWidth(90)
        self.export_csv_btn.setEnabled(False)
        layout.addWidget(self.export_csv_btn)
        
        self.export_pdf_btn = QtWidgets.QPushButton("📊 PDF")
        self.export_pdf_btn.setMinimumWidth(90)
        self.export_pdf_btn.setEnabled(False)
        layout.addWidget(self.export_pdf_btn)
        
        layout.addSpacing(10)
        
        self.clear_btn = QtWidgets.QPushButton("🗑 Clear")
        self.clear_btn.setProperty("class", "danger")
        self.clear_btn.setMinimumWidth(90)
        layout.addWidget(self.clear_btn)
        
        parent.addWidget(frame)
    
    def _create_status_bar(self, parent: QtWidgets.QVBoxLayout) -> None:
        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(4, 0, 4, 0)
        
        self.status_label = QtWidgets.QLabel("● Disconnected")
        self.status_label.setStyleSheet(f"color: {self.theme.text_muted};")
        layout.addWidget(self.status_label)
        
        layout.addStretch()
        
        # Cursor values display
        self.cursor_label = QtWidgets.QLabel("")
        self.cursor_label.setStyleSheet(f"color: {self.theme.text_secondary}; font-family: monospace;")
        layout.addWidget(self.cursor_label)
        
        layout.addSpacing(20)
        
        self.samples_label = QtWidgets.QLabel("Samples: 0")
        self.samples_label.setStyleSheet(f"color: {self.theme.text_secondary};")
        layout.addWidget(self.samples_label)
        
        parent.addLayout(layout)
    
    def _connect_signals(self) -> None:
        self.refresh_btn.clicked.connect(self._refresh_ports)
        self.show_all_cb.stateChanged.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self._start_acquisition)
        self.stop_btn.clicked.connect(self._stop_acquisition)
        
        self.import_btn.clicked.connect(self._import_csv)
        self.select_all_btn.clicked.connect(self._select_all)
        self.export_csv_btn.clicked.connect(self._export_csv)
        self.export_pdf_btn.clicked.connect(self._export_pdf)
        self.clear_btn.clicked.connect(self._clear_data)
        
        self.settings_btn.clicked.connect(self._open_settings)
        
        # Connect plot view changes (pan, zoom) to trigger re-render
        self.plot_widget.view_changed.connect(self._request_plot_update)
        
        # Connect cursor values signal
        self.plot_widget.cursor_values.connect(self._on_cursor_values)
    
    def _on_cursor_values(self, t: float, v: float, i: float, p: float) -> None:
        """Update cursor values display."""
        self.cursor_label.setText(
            f"T: {t:.3f}s | V: {v:.4f}V | I: {i:.4f}A | P: {p:.4f}W"
        )
    
    def _setup_port_monitor(self) -> None:
        """Setup OS-level port change monitoring for auto-reconnect."""
        import sys
        if sys.platform == 'linux':
            self._setup_linux_port_monitor()
        else:
            # Fallback: periodic check (less efficient but works everywhere)
            self._port_check_timer = QtCore.QTimer()
            self._port_check_timer.timeout.connect(self._check_port_availability)
            self._port_check_timer.start(2000)  # Check every 2 seconds
    
    def _setup_linux_port_monitor(self) -> None:
        """Use inotify to watch for USB device changes on Linux."""
        try:
            from PySide6.QtCore import QSocketNotifier
            import os
            
            # Watch /dev for device changes
            # We use a simpler approach: QFileSystemWatcher on /dev/serial/by-id
            from PySide6.QtCore import QFileSystemWatcher
            
            self._port_watcher = QFileSystemWatcher()
            
            # Watch common serial device directories
            watch_paths = ['/dev/serial/by-id', '/dev/serial/by-path', '/dev']
            for path in watch_paths:
                if os.path.exists(path):
                    self._port_watcher.addPath(path)
            
            self._port_watcher.directoryChanged.connect(self._on_port_change)
        except Exception:
            # Fallback to timer-based checking
            self._port_check_timer = QtCore.QTimer()
            self._port_check_timer.timeout.connect(self._check_port_availability)
            self._port_check_timer.start(2000)
    
    def _on_port_change(self, path: str) -> None:
        """Called when serial port directory changes."""
        # Refresh port list
        self._refresh_ports()
        
        # Try to reconnect if we were disconnected and auto-reconnect is enabled
        if (self.settings.auto_reconnect and 
            self._last_port and 
            not self._acquiring and
            not self._is_connected()):
            self._try_reconnect()
    
    def _check_port_availability(self) -> None:
        """Periodic check for port changes (fallback for non-Linux)."""
        if not self.settings.auto_reconnect:
            return
        
        if self._last_port and not self._acquiring and not self._is_connected():
            self._try_reconnect()
    
    def _try_reconnect(self) -> None:
        """Attempt to reconnect to the last used port."""
        if not self._last_port:
            return
        
        # Check if port is available
        from serial.tools import list_ports
        available = [p.device for p in list_ports.comports()]
        
        if self._last_port in available:
            self.status_label.setText(f"● Reconnecting to {self._last_port}...")
            self.status_label.setStyleSheet(f"color: {self.theme.accent_warning};")
            
            # Set port in combo box
            for i in range(self.port_combo.count()):
                if self.port_combo.itemData(i) == self._last_port:
                    self.port_combo.setCurrentIndex(i)
                    break
            
            # Start acquisition
            QtCore.QTimer.singleShot(500, self._start_acquisition)
    
    def _setup_plot_throttle(self) -> None:
        """Setup event-driven plot updates with rate limiting."""
        # Timer fires only when plot is dirty, with rate limiting
        self._plot_timer = QtCore.QTimer()
        self._plot_timer.setSingleShot(True)
        self._plot_timer.timeout.connect(self._do_plot_update)
    
    def _request_plot_update(self) -> None:
        """Request a plot update (rate-limited to 60 FPS max)."""
        now = time.perf_counter()
        elapsed_ms = (now - self._last_plot_update) * 1000
        
        if elapsed_ms >= self.MIN_PLOT_INTERVAL_MS:
            self._do_plot_update()
        elif not self._plot_timer.isActive():
            wait_ms = int(self.MIN_PLOT_INTERVAL_MS - elapsed_ms) + 1
            self._plot_timer.start(wait_ms)
    
    def _do_plot_update(self) -> None:
        """Actually perform the plot update."""
        self._plot_dirty = False
        self._last_plot_update = time.perf_counter()
        
        # Always update with current buffers (works during and after acquisition)
        if not self.buffers.is_empty:
            self.plot_widget.update_data(self.buffers)
        self._update_selection_stats()
    
    # -------------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------------
    
    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self.theme, self)
        dialog.theme_changed.connect(self._on_theme_changed)
        dialog.settings_changed.connect(self._on_settings_changed)
        dialog.exec()
    
    def _on_theme_changed(self, dark_mode: bool) -> None:
        self.theme = DARK_THEME if dark_mode else LIGHT_THEME
        self.settings.dark_mode = dark_mode
        self._apply_theme()
        self._update_theme_widgets()
    
    def _update_theme_widgets(self) -> None:
        """Update widgets with theme colors."""
        self.plot_widget.update_theme(self.theme)
        
        connected = self._is_connected()
        self.status_label.setStyleSheet(
            f"color: {self.theme.accent_success if connected else self.theme.text_muted};"
        )
        self.samples_label.setStyleSheet(f"color: {self.theme.text_secondary};")
        self.sel_samples_label.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        self.sel_duration_label.setStyleSheet(f"color: {self.theme.text_secondary}; font-size: 11px;")
        self.sel_power_label.setStyleSheet(f"color: {self.theme.chart_power}; font-size: 11px;")
        
        self.voltage_card.value_label.setStyleSheet(f"color: {self.theme.chart_voltage};")
        self.current_card.value_label.setStyleSheet(f"color: {self.theme.chart_current};")
        self.power_card.value_label.setStyleSheet(f"color: {self.theme.chart_power};")
    
    def _on_settings_changed(self, settings: AppSettings) -> None:
        self.settings = settings
        self.buffers.max_points = settings.plot_points
        
        # Update power window size for moving average
        new_window = deque(self._power_window, maxlen=settings.moving_average_window)
        self._power_window = new_window
        
        # Update plot widget settings
        self.plot_widget.set_grid(settings.show_grid, settings.grid_alpha)
        self.plot_widget.set_crosshair(settings.show_crosshair)
        
        # Update report generator FFT setting
        self.report_generator.include_fft = settings.include_fft
        
        # Save settings to persistent storage
        settings.save()
        
        # Request plot update to apply any visual changes
        self._request_plot_update()
    
    # -------------------------------------------------------------------------
    # Connection
    # -------------------------------------------------------------------------
    
    def _refresh_ports(self) -> None:
        self.port_combo.clear()
        for device, label in PortDiscovery.get_ports(self.show_all_cb.isChecked()):
            self.port_combo.addItem(label, device)
    
    def _toggle_connection(self) -> None:
        if self._is_connected():
            self._stop_acquisition()
        else:
            self._start_acquisition()
    
    def _is_connected(self) -> bool:
        return self.reader is not None and self.reader.isRunning()
    
    def _start_acquisition(self) -> None:
        port = self.port_combo.currentData()
        if not port:
            QtWidgets.QMessageBox.warning(self, "Error", "Select a serial port first.")
            return
        
        # Save port for auto-reconnect
        self._last_port = port
        
        # Make sure any previous reader is fully stopped
        if self.reader is not None:
            try:
                self.reader.data_received.disconnect(self._on_data)
                self.reader.error.disconnect(self._on_error)
            except RuntimeError:
                pass
            self.reader.stop(self.STOP_TIMEOUT_MS)
            self.reader = None
        
        # Clear data BEFORE starting reader to avoid race condition
        self._clear_data()
        
        # Longer delay to ensure port is released and buffers are clean
        QtCore.QCoreApplication.processEvents()
        QtCore.QThread.msleep(200)
        
        # Create and start new reader
        self.reader = SerialReader(port, baud=self.settings.baud_rate)
        self.reader.data_received.connect(self._on_data, QtCore.Qt.QueuedConnection)
        self.reader.error.connect(self._on_error, QtCore.Qt.QueuedConnection)
        self.reader.start()
        
        # Now we're acquiring - record start time
        self._acquiring = True
        self._acq_start_time = time.perf_counter()
        
        self.connect_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        
        # Disable PDF export during acquisition (CSV stays disabled from clear)
        self.export_pdf_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.select_all_btn.setEnabled(False)
        
        self.status_label.setText(f"● Connected: {port}")
        self.status_label.setStyleSheet(f"color: {self.theme.accent_success};")
    
    def _stop_acquisition(self) -> None:
        # Stop acquiring first
        self._acquiring = False
        
        if self.reader:
            # Disconnect signals first to avoid race conditions
            try:
                self.reader.data_received.disconnect(self._on_data)
                self.reader.error.disconnect(self._on_error)
            except RuntimeError:
                pass  # Already disconnected
            
            self.reader.stop(self.STOP_TIMEOUT_MS)
            self.reader = None
        
        self.connect_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        
        self.status_label.setText("● Disconnected")
        self.status_label.setStyleSheet(f"color: {self.theme.text_muted};")
        
        # Enable export after stopping if we have data
        if self.full_data:
            # Small delay to ensure plot is updated before showing region selector
            QtCore.QTimer.singleShot(100, self._enable_export)
    
    # -------------------------------------------------------------------------
    # Data Handling
    # -------------------------------------------------------------------------
    
    def _on_data(self, data: dict) -> None:
        # Ignore data if we're not actively acquiring
        if not self._acquiring:
            return
        
        # Use local perf_counter for relative time (reliable, no RTC issues)
        rel_time = time.perf_counter() - self._acq_start_time
        
        # Keep firmware timestamp for export
        ts = data['timestamp']
        unix_time = ts.timestamp() if ts else time.time()
        
        v, i, p = data['voltage'], data['current'], data['power']
        
        # Use relative time for plotting
        self.buffers.append(rel_time, v, i, p)
        
        self.full_data.append(MeasurementRecord(ts, unix_time, rel_time, v, i, p))
        
        # Update running statistics
        self._power_sum += p
        self._power_window.append(p)
        
        # Request plot update (rate-limited to 60 FPS)
        self._request_plot_update()
        
        # Update stat cards every 5 samples (~20Hz at 100Hz input)
        n = len(self.full_data)
        if n % 5 == 0:
            self._update_stat_cards(v, i, p)
        
        # Update sample count every 50 samples
        if n % 50 == 0:
            self.samples_label.setText(f"Samples: {n:,}")
    
    def _update_stat_cards(self, v: float, i: float, p: float) -> None:
        """Update the stat cards with current values."""
        self.voltage_card.set_value(v)
        self.current_card.set_value(i)
        self.power_card.set_value(p)
        self.avg_power_card.set_value(self._calculate_avg_power())
    
    def _calculate_avg_power(self) -> float:
        """Calculate average power efficiently using running statistics."""
        if not self.full_data:
            return 0.0
        
        if self.settings.use_moving_average:
            # Moving average using deque - O(1)
            if self._power_window:
                return sum(self._power_window) / len(self._power_window)
            return 0.0
        else:
            # Total average using running sum - O(1)
            return self._power_sum / len(self.full_data)
    
    def _on_error(self, msg: str) -> None:
        QtWidgets.QMessageBox.critical(self, "Serial Error", msg)
        self._stop_acquisition()
    
    def _clear_data(self) -> None:
        self.buffers.clear()
        self.full_data = []
        
        # Reset running statistics
        self._power_sum = 0.0
        self._power_window.clear()
        
        self.plot_widget.clear_data()
        self.select_all_btn.setEnabled(False)
        
        # Disable and remove colored classes (back to grey)
        self.export_csv_btn.setEnabled(False)
        self.export_csv_btn.setProperty("class", "")
        self.export_csv_btn.style().unpolish(self.export_csv_btn)
        self.export_csv_btn.style().polish(self.export_csv_btn)
        
        self.export_pdf_btn.setEnabled(False)
        self.export_pdf_btn.setProperty("class", "")
        self.export_pdf_btn.style().unpolish(self.export_pdf_btn)
        self.export_pdf_btn.style().polish(self.export_pdf_btn)
        
        self.samples_label.setText("Samples: 0")
        self.voltage_card.set_value(0)
        self.current_card.set_value(0)
        self.power_card.set_value(0)
        self.avg_power_card.set_value(0)
        
        self.sel_samples_label.setText("Samples: --")
        self.sel_duration_label.setText("Duration: --")
        self.sel_power_label.setText("Avg Power: --")
    
    # -------------------------------------------------------------------------
    # Selection & Export
    # -------------------------------------------------------------------------
    
    def _enable_export(self) -> None:
        if not self.full_data:
            return
        
        # Use relative time for region selector (starts from 0)
        t_min = self.full_data[0].relative_time
        t_max = self.full_data[-1].relative_time
        
        # Reset view to show all data before adding region selector
        self.plot_widget.show_full_range(t_min, t_max)
        self.plot_widget.add_region_selector(t_min, t_max)
        
        self.select_all_btn.setEnabled(True)
        
        # Enable with colors: CSV green, PDF blue
        self.export_csv_btn.setEnabled(True)
        self.export_csv_btn.setProperty("class", "success")
        self.export_csv_btn.style().unpolish(self.export_csv_btn)
        self.export_csv_btn.style().polish(self.export_csv_btn)
        
        self.export_pdf_btn.setEnabled(True)
        self.export_pdf_btn.setProperty("class", "primary")
        self.export_pdf_btn.style().unpolish(self.export_pdf_btn)
        self.export_pdf_btn.style().polish(self.export_pdf_btn)
    
    def _select_all(self) -> None:
        if not self.full_data:
            return
        t_min = self.full_data[0].relative_time
        t_max = self.full_data[-1].relative_time
        self.plot_widget.add_region_selector(t_min, t_max)
    
    def _get_selected_records(self) -> List[MeasurementRecord]:
        time_range = self.plot_widget.get_selected_range()
        if not time_range:
            return self.full_data
        t0, t1 = time_range
        # Filter by relative time (matches the plot axis)
        return [r for r in self.full_data if t0 <= r.relative_time <= t1]
    
    def _update_selection_stats(self) -> None:
        records = self._get_selected_records()
        
        if len(records) < 2:
            self.sel_samples_label.setText("Samples: --")
            self.sel_duration_label.setText("Duration: --")
            self.sel_power_label.setText("Avg Power: --")
            return
        
        # Use absolute difference to handle any ordering issues
        duration = abs(records[-1].unix_time - records[0].unix_time)
        avg_power = sum(r.power for r in records) / len(records)
        
        self.sel_samples_label.setText(f"Samples: {len(records):,}")
        if duration < 60:
            self.sel_duration_label.setText(f"Duration: {duration:.1f}s")
        elif duration < 3600:
            self.sel_duration_label.setText(f"Duration: {duration/60:.1f}m")
        else:
            self.sel_duration_label.setText(f"Duration: {duration/3600:.1f}h")
        self.sel_power_label.setText(f"Avg Power: {avg_power:.3f} W")
    
    def _import_csv(self) -> None:
        """Import measurements from a CSV file."""
        if self._is_connected():
            QtWidgets.QMessageBox.warning(
                self, "Warning", 
                "Stop acquisition before importing data."
            )
            return
        
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import CSV", "", 
            "CSV Files (*.csv);;Text Files (*.txt);;All Files (*)"
        )
        
        if not path:
            return
        
        try:
            records = CSVImporter.import_csv(Path(path))
            
            # Clear existing data and load imported
            self._clear_data()
            self.full_data = records
            
            # Populate plot buffers
            for r in records:
                self.buffers.append(r.unix_time, r.voltage, r.current, r.power)

            # Update plot
            self._do_plot_update()
            self._enable_export()
            
            if records:
                self.voltage_card.set_value(records[-1].voltage)
                self.current_card.set_value(records[-1].current)
                self.power_card.set_value(records[-1].power)
            
            self.samples_label.setText(f"Samples: {len(records):,}")
            self.status_label.setText(f"● Imported: {Path(path).name}")
            self.status_label.setStyleSheet(f"color: {self.theme.accent_primary};")
            
            # Calculate duration for message
            if len(records) >= 2:
                duration = records[-1].unix_time - records[0].unix_time
                duration_str = f"{duration:.1f}s" if duration < 60 else f"{duration/60:.1f}m"
            else:
                duration_str = "N/A"
            
            QtWidgets.QMessageBox.information(
                self, "Import Successful",
                f"Imported {len(records):,} samples\n"
                f"Duration: {duration_str}\n"
                f"From: {Path(path).name}"
            )
            
        except FileNotFoundError as e:
            QtWidgets.QMessageBox.critical(self, "Error", str(e))
        except ValueError as e:
            QtWidgets.QMessageBox.critical(
                self, "Import Error", 
                f"Could not parse CSV file:\n{e}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Error", 
                f"Unexpected error during import:\n{e}"
            )
    
    def _export_csv(self) -> None:
        records = self._get_selected_records()
        if not records:
            QtWidgets.QMessageBox.warning(self, "Error", "No data to export.")
            return
        
        default_name = f"measurement_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export CSV", default_name, "CSV Files (*.csv)"
        )
        
        if not path:
            return
        
        # Run export with progress dialog
        self._run_export(
            lambda: self.report_generator.export_csv(Path(path), records, self.settings.csv_separator),
            f"Exported {len(records):,} samples to:\n{path}",
            "Exporting CSV..."
        )
    
    def _export_pdf(self) -> None:
        records = self._get_selected_records()
        if len(records) < 2:
            QtWidgets.QMessageBox.warning(self, "Error", "Need at least 2 samples.")
            return
        
        stats = Statistics.from_records(records)
        if not stats:
            QtWidgets.QMessageBox.warning(self, "Error", "Could not calculate statistics.")
            return
        
        default_name = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export PDF", default_name, "PDF Files (*.pdf)"
        )
        
        if not path:
            return
        
        summary = (
            f"Report generated!\n\n"
            f"Samples: {stats.count:,}\n"
            f"Duration: {stats.duration_seconds:.1f}s\n"
            f"Avg Power: {stats.power_avg:.4f} W\n"
            f"Energy: {stats.energy_wh*1000:.4f} mWh\n\n"
            f"Saved to:\n{path}"
        )
        
        # Run export with progress dialog
        self._run_export(
            lambda: self.report_generator.export_pdf(Path(path), stats, records),
            summary,
            "Generating PDF report..."
        )
    
    def _run_export(self, export_func, success_msg: str, progress_msg: str) -> None:
        """Run export function in background thread with progress dialog."""
        from PySide6.QtCore import QThread, Signal
        
        class ExportWorker(QThread):
            finished = Signal(bool, str)  # success, error_message
            
            def __init__(self, func):
                super().__init__()
                self.func = func
            
            def run(self):
                try:
                    self.func()
                    self.finished.emit(True, "")
                except Exception as e:
                    self.finished.emit(False, str(e))
        
        progress = QtWidgets.QProgressDialog(progress_msg, None, 0, 0, self)
        progress.setWindowTitle("Exporting")
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)
        progress.show()
        
        def on_finished(success: bool, error: str):
            progress.close()
            worker.deleteLater()
            if success:
                QtWidgets.QMessageBox.information(self, "Success", success_msg)
            else:
                QtWidgets.QMessageBox.critical(self, "Error", f"Export failed: {error}")
        
        worker = ExportWorker(export_func)
        worker.finished.connect(on_finished)
        worker.start()
    
    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.reader:
            self.reader.stop(self.STOP_TIMEOUT_MS)
        event.accept()
