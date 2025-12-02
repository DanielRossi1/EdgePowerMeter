"""Main window for EdgePowerMeter application.

Clean, modular UI for real-time power monitoring with data analysis
and export capabilities.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Deque

from PySide6 import QtCore, QtWidgets, QtGui

from app.serial.reader import SerialReader
from app.version import __version__, APP_NAME
from .theme import ThemeColors, DARK_THEME, LIGHT_THEME, generate_stylesheet
from .settings import SettingsDialog, AppSettings
from .report import ReportGenerator, Statistics, MeasurementRecord, CSVImporter
from .widgets import PlotBuffers, PlotWidget, StatCard, PortDiscovery


# =============================================================================
# Main Window
# =============================================================================

class MainWindow(QtWidgets.QMainWindow):
    """Main application window."""
    
    PLOT_UPDATE_MS = 100  # Update plots every 100ms (10 FPS) for smooth performance
    STOP_TIMEOUT_MS = 3000
    MAX_DATA_POINTS = 100000  # Max points before warning
    
    def __init__(self):
        super().__init__()
        self._init_state()
        self._setup_ui()
        self._connect_signals()
        self._start_timers()
        self._refresh_ports()
    
    def _init_state(self) -> None:
        self.reader: Optional[SerialReader] = None
        self.buffers = PlotBuffers()
        self.full_data: List[MeasurementRecord] = []
        self.report_generator = ReportGenerator()
        self.settings = AppSettings()
        self.theme = DARK_THEME
        
        # Running statistics for efficient avg power calculation
        self._power_sum: float = 0.0
        self._power_window: Deque[float] = deque(maxlen=1000)  # For moving average
    
    def _setup_ui(self) -> None:
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.setMinimumSize(1200, 800)
        self.resize(1400, 900)
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
        
        self.connect_btn = QtWidgets.QPushButton("▶ Start")
        self.connect_btn.setProperty("class", "success")
        self.connect_btn.setMinimumWidth(110)
        layout.addWidget(self.connect_btn)
        
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
        
        self.stop_btn = QtWidgets.QPushButton("⏹ Stop")
        self.stop_btn.setMinimumWidth(90)
        self.stop_btn.setEnabled(False)
        layout.addWidget(self.stop_btn)
        
        layout.addSpacing(10)
        
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
        self.export_pdf_btn.setProperty("class", "primary")
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
        
        self.samples_label = QtWidgets.QLabel("Samples: 0")
        self.samples_label.setStyleSheet(f"color: {self.theme.text_secondary};")
        layout.addWidget(self.samples_label)
        
        parent.addLayout(layout)
    
    def _connect_signals(self) -> None:
        self.refresh_btn.clicked.connect(self._refresh_ports)
        self.show_all_cb.stateChanged.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self._toggle_connection)
        self.stop_btn.clicked.connect(self._stop_acquisition)
        
        self.import_btn.clicked.connect(self._import_csv)
        self.select_all_btn.clicked.connect(self._select_all)
        self.export_csv_btn.clicked.connect(self._export_csv)
        self.export_pdf_btn.clicked.connect(self._export_pdf)
        self.clear_btn.clicked.connect(self._clear_data)
        
        self.settings_btn.clicked.connect(self._open_settings)
    
    def _start_timers(self) -> None:
        self.plot_timer = QtCore.QTimer()
        self.plot_timer.setInterval(self.PLOT_UPDATE_MS)
        self.plot_timer.timeout.connect(self._update_ui)
        self.plot_timer.start()
    
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
        
        self.reader = SerialReader(port, baud=self.settings.baud_rate)
        self.reader.data_received.connect(self._on_data)
        self.reader.error.connect(self._on_error)
        self.reader.start()
        
        self._clear_data()
        
        self.connect_btn.setText("⏹ Stop")
        self.connect_btn.setProperty("class", "danger")
        self.connect_btn.setStyle(self.connect_btn.style())
        
        self.stop_btn.setEnabled(True)
        
        self.status_label.setText(f"● Connected: {port}")
        self.status_label.setStyleSheet(f"color: {self.theme.accent_success};")
    
    def _stop_acquisition(self) -> None:
        if self.reader:
            self.reader.stop(self.STOP_TIMEOUT_MS)
            self.reader = None
        
        self.connect_btn.setText("▶ Start")
        self.connect_btn.setProperty("class", "success")
        self.connect_btn.setStyle(self.connect_btn.style())
        
        self.stop_btn.setEnabled(False)
        
        self.status_label.setText("● Disconnected")
        self.status_label.setStyleSheet(f"color: {self.theme.text_muted};")
        
        if self.full_data:
            self._enable_export()
    
    # -------------------------------------------------------------------------
    # Data Handling
    # -------------------------------------------------------------------------
    
    def _on_data(self, data: dict) -> None:
        ts = data['timestamp']
        try:
            t = ts.timestamp()
        except Exception:
            t = datetime.now().timestamp()
        
        v, i, p = data['voltage'], data['current'], data['power']
        
        self.buffers.append(t, v, i, p)
        self.full_data.append(MeasurementRecord(ts, t, v, i, p))
        
        # Update running statistics
        self._power_sum += p
        self._power_window.append(p)
        
        # Update UI cards (lightweight)
        self.voltage_card.set_value(v)
        self.current_card.set_value(i)
        self.power_card.set_value(p)
        
        # Calculate average power efficiently
        avg_power = self._calculate_avg_power()
        self.avg_power_card.set_value(avg_power)
        
        # Update sample count less frequently (every 10 samples)
        n = len(self.full_data)
        if n % 10 == 0:
            self.samples_label.setText(f"Samples: {n:,}")
    
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
    
    def _update_ui(self) -> None:
        self.plot_widget.update_data(self.buffers)
        self._update_selection_stats()
    
    def _clear_data(self) -> None:
        self.buffers.clear()
        self.full_data = []
        
        # Reset running statistics
        self._power_sum = 0.0
        self._power_window.clear()
        
        self.plot_widget.clear_data()
        self.select_all_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.export_pdf_btn.setEnabled(False)
        
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
        
        t_min = self.full_data[0].unix_time
        t_max = self.full_data[-1].unix_time
        self.plot_widget.add_region_selector(t_min, t_max)
        
        self.select_all_btn.setEnabled(True)
        self.export_csv_btn.setEnabled(True)
        self.export_pdf_btn.setEnabled(True)
    
    def _select_all(self) -> None:
        if not self.full_data:
            return
        t_min = self.full_data[0].unix_time
        t_max = self.full_data[-1].unix_time
        self.plot_widget.add_region_selector(t_min, t_max)
    
    def _get_selected_records(self) -> List[MeasurementRecord]:
        time_range = self.plot_widget.get_selected_range()
        if not time_range:
            return self.full_data
        t0, t1 = time_range
        return [r for r in self.full_data if t0 <= r.unix_time <= t1]
    
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
            
            # Update UI
            self._update_ui()
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
        """Run export function with progress dialog."""
        progress = QtWidgets.QProgressDialog(progress_msg, None, 0, 0, self)
        progress.setWindowTitle("Exporting")
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)
        progress.show()
        
        # Process events to show dialog
        QtCore.QCoreApplication.processEvents()
        
        try:
            export_func()
            progress.close()
            QtWidgets.QMessageBox.information(self, "Success", success_msg)
        except Exception as e:
            progress.close()
            QtWidgets.QMessageBox.critical(self, "Error", f"Export failed: {e}")
    
    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.reader:
            self.reader.stop(self.STOP_TIMEOUT_MS)
        event.accept()
