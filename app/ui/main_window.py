"""Main window for EdgePowerMeter application.

Clean, modular UI for real-time power monitoring with data analysis
and export capabilities.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, List, Optional, Tuple

from PySide6 import QtCore, QtWidgets, QtGui
import pyqtgraph as pg
from pyqtgraph.graphicsItems.DateAxisItem import DateAxisItem
from serial.tools import list_ports

from app.serial.reader import SerialReader
from app.version import __version__, APP_NAME
from .theme import ThemeColors, DARK_THEME, LIGHT_THEME, generate_stylesheet
from .settings import SettingsDialog, AppSettings
from .report import ReportGenerator, Statistics, MeasurementRecord


# =============================================================================
# Plot Buffer
# =============================================================================

class PlotBuffers:
    """Manages plot data buffers with millisecond precision timestamps.
    
    Each measurement is stored as a separate point. Out-of-order or
    duplicate timestamps are discarded to prevent plot artifacts.
    """
    
    def __init__(self, max_points: int = 5000):
        self.max_points = max_points
        self.timestamps: Deque[float] = deque(maxlen=max_points)
        self.voltages: Deque[float] = deque(maxlen=max_points)
        self.currents: Deque[float] = deque(maxlen=max_points)
        self.powers: Deque[float] = deque(maxlen=max_points)
    
    def append(self, t: float, v: float, i: float, p: float) -> None:
        """Append a sample. Discards if timestamp <= last (out of order)."""
        # Only accept strictly increasing timestamps
        if self.timestamps and t <= self.timestamps[-1]:
            return
        
        self.timestamps.append(t)
        self.voltages.append(v)
        self.currents.append(i)
        self.powers.append(p)
    
    def clear(self) -> None:
        """Clear all buffers."""
        self.timestamps.clear()
        self.voltages.clear()
        self.currents.clear()
        self.powers.clear()
    
    @property
    def is_empty(self) -> bool:
        return len(self.timestamps) == 0


# =============================================================================
# UI Components
# =============================================================================

class StatCard(QtWidgets.QFrame):
    """Compact stat display card."""
    
    def __init__(self, label: str, unit: str, color: str, parent=None):
        super().__init__(parent)
        self.setProperty("class", "stat-card")
        self.color = color
        self._setup_ui(label, unit)
    
    def _setup_ui(self, label: str, unit: str) -> None:
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
        self.value_label.setText(f"{value:.{decimals}f}")


class PlotWidget(pg.GraphicsLayoutWidget):
    """Three-panel plot with independent zoom."""
    
    def __init__(self, theme: ThemeColors, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.setBackground(theme.bg_secondary)
        self._setup_plots()
        self.region: Optional[pg.LinearRegionItem] = None
    
    def _setup_plots(self) -> None:
        pg.setConfigOptions(antialias=True)
        
        self.plot_v = self.addPlot(
            row=0, col=0, title="Voltage [V]",
            axisItems={'bottom': DateAxisItem(orientation='bottom')}
        )
        self._style_plot(self.plot_v, self.theme.chart_voltage)
        self.curve_v = self.plot_v.plot(pen=pg.mkPen(self.theme.chart_voltage, width=2))
        
        self.nextRow()
        
        self.plot_i = self.addPlot(
            row=1, col=0, title="Current [A]",
            axisItems={'bottom': DateAxisItem(orientation='bottom')}
        )
        self._style_plot(self.plot_i, self.theme.chart_current)
        self.curve_i = self.plot_i.plot(pen=pg.mkPen(self.theme.chart_current, width=2))
        
        self.nextRow()
        
        self.plot_p = self.addPlot(
            row=2, col=0, title="Power [W]",
            axisItems={'bottom': DateAxisItem(orientation='bottom')}
        )
        self._style_plot(self.plot_p, self.theme.chart_power)
        self.curve_p = self.plot_p.plot(pen=pg.mkPen(self.theme.chart_power, width=2))
    
    def _style_plot(self, plot: pg.PlotItem, color: str) -> None:
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.getAxis('left').setTextPen(self.theme.text_primary)
        plot.getAxis('left').setPen(self.theme.border_default)
        plot.getAxis('bottom').setTextPen(self.theme.text_primary)
        plot.getAxis('bottom').setPen(self.theme.border_default)
        plot.setTitle(plot.titleLabel.text, color=color, size='11pt')
        plot.getViewBox().setBackgroundColor(self.theme.bg_secondary)
    
    def update_data(self, buffers: PlotBuffers) -> None:
        if buffers.is_empty:
            return
        xs = list(buffers.timestamps)
        self.curve_v.setData(xs, list(buffers.voltages))
        self.curve_i.setData(xs, list(buffers.currents))
        self.curve_p.setData(xs, list(buffers.powers))
    
    def add_region_selector(self, t_min: float, t_max: float) -> None:
        self.remove_region_selector()
        self.region = pg.LinearRegionItem(
            values=(t_min, t_max),
            brush=pg.mkBrush(self.theme.accent_primary + "30"),
            pen=pg.mkPen(self.theme.accent_primary, width=2),
        )
        self.region.setZValue(10)
        self.plot_p.addItem(self.region)
    
    def remove_region_selector(self) -> None:
        if self.region:
            try:
                self.plot_p.removeItem(self.region)
            except Exception:
                pass
            self.region = None
    
    def get_selected_range(self) -> Optional[Tuple[float, float]]:
        if not self.region:
            return None
        return tuple(sorted(self.region.getRegion()))
    
    def update_theme(self, theme: ThemeColors) -> None:
        """Update all theme-dependent colors."""
        self.theme = theme
        self.setBackground(theme.bg_secondary)
        
        for plot, color in [
            (self.plot_v, theme.chart_voltage),
            (self.plot_i, theme.chart_current),
            (self.plot_p, theme.chart_power)
        ]:
            plot.getAxis('left').setTextPen(theme.text_primary)
            plot.getAxis('left').setPen(theme.border_default)
            plot.getAxis('bottom').setTextPen(theme.text_primary)
            plot.getAxis('bottom').setPen(theme.border_default)
            plot.setTitle(plot.titleLabel.text, color=color, size='11pt')
            plot.getViewBox().setBackgroundColor(theme.bg_secondary)
        
        self.curve_v.setPen(pg.mkPen(theme.chart_voltage, width=2))
        self.curve_i.setPen(pg.mkPen(theme.chart_current, width=2))
        self.curve_p.setPen(pg.mkPen(theme.chart_power, width=2))
        
        if self.region:
            self.region.setBrush(pg.mkBrush(theme.accent_primary + "30"))
            self.region.setPen(pg.mkPen(theme.accent_primary, width=2))


class PortDiscovery:
    """Serial port discovery utility."""
    
    USB_MARKERS = ['USB', 'ACM', 'FTDI', 'CP210', 'CH340', 'PL2303']
    DEVICE_PATTERN = re.compile(r'ttyUSB|ttyACM|ttyAMA|cu\.usb|COM\d+', re.I)
    
    @classmethod
    def get_ports(cls, show_all: bool = False) -> List[Tuple[str, str]]:
        result = []
        for port in list_ports.comports():
            if show_all or cls._is_usb_device(port):
                desc = port.description or port.hwid or 'Unknown'
                result.append((port.device, f"{port.device} — {desc}"))
        return result
    
    @classmethod
    def _is_usb_device(cls, port) -> bool:
        if getattr(port, 'vid', None) is not None:
            return True
        text = f"{port.description or ''} {port.hwid or ''}".upper()
        return any(m in text for m in cls.USB_MARKERS) or bool(cls.DEVICE_PATTERN.search(port.device))


# =============================================================================
# Main Window
# =============================================================================

class MainWindow(QtWidgets.QMainWindow):
    """Main application window."""
    
    PLOT_UPDATE_MS = 50
    STOP_TIMEOUT_MS = 3000
    
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
        
        self.voltage_card.set_value(v)
        self.current_card.set_value(i)
        self.power_card.set_value(p)
        
        self.samples_label.setText(f"Samples: {len(self.full_data):,}")
    
    def _on_error(self, msg: str) -> None:
        QtWidgets.QMessageBox.critical(self, "Serial Error", msg)
        self._stop_acquisition()
    
    def _update_ui(self) -> None:
        self.plot_widget.update_data(self.buffers)
        self._update_selection_stats()
    
    def _clear_data(self) -> None:
        self.buffers.clear()
        self.full_data = []
        
        self.plot_widget.remove_region_selector()
        self.select_all_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.export_pdf_btn.setEnabled(False)
        
        self.samples_label.setText("Samples: 0")
        self.voltage_card.set_value(0)
        self.current_card.set_value(0)
        self.power_card.set_value(0)
        
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
        
        duration = records[-1].unix_time - records[0].unix_time
        avg_power = sum(r.power for r in records) / len(records)
        
        self.sel_samples_label.setText(f"Samples: {len(records):,}")
        self.sel_duration_label.setText(
            f"Duration: {duration:.1f}s" if duration < 60 else f"Duration: {duration/60:.1f}m"
        )
        self.sel_power_label.setText(f"Avg Power: {avg_power:.3f} W")
    
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
        
        try:
            self.report_generator.export_csv(
                Path(path), records, self.settings.csv_separator
            )
            QtWidgets.QMessageBox.information(
                self, "Success", f"Exported {len(records):,} samples to:\n{path}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Export failed: {e}")
    
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
        
        try:
            self.report_generator.export_pdf(Path(path), stats, records)
            
            summary = (
                f"Report generated!\n\n"
                f"Samples: {stats.count:,}\n"
                f"Duration: {stats.duration_seconds:.1f}s\n"
                f"Avg Power: {stats.power_avg:.4f} W\n"
                f"Energy: {stats.energy_wh*1000:.4f} mWh\n\n"
                f"Saved to:\n{path}"
            )
            QtWidgets.QMessageBox.information(self, "Success", summary)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"PDF generation failed: {e}")
    
    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.reader:
            self.reader.stop(self.STOP_TIMEOUT_MS)
        event.accept()
