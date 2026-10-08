"""Main window: navigation rail, connection bar, pages and status bar."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..core import AppSettings, CPUUsageMonitor
from ..i18n import set_language, tr
from ..version import APP_NAME, __version__
from .controller import (STATE_CONNECTING, STATE_IDLE, STATE_RECONNECTING, STATE_STALE,
                         STATE_STREAMING, AcquisitionController)
from .file_actions import FileActions
from .marker_actions import MarkerActions
from .icons import icon
from .pages.about_page import AboutPage
from .pages.analysis_page import AnalysisPage
from .pages.device_page import DevicePage
from .pages.live_page import LivePage
from .pages.settings_page import SettingsPage
from .theme import apply_theme, resolve_theme
from .widgets.common import Banner, button, confirm, label, set_variant
from .widgets.cpu_bar import CPUBar
from .widgets.port_discovery import PortDiscovery

PAGE_LIVE, PAGE_ANALYSIS, PAGE_DEVICE, PAGE_SETTINGS, PAGE_ABOUT = range(5)

# Settings whose change requires rebuilding the widgets (texts or colors).
_REBUILD_KEYS = {"language", "theme", "*"}


class MainWindow(QtWidgets.QMainWindow):
    METRICS_INTERVAL_MS = 125
    SLOW_INTERVAL_MS = 1000
    PORT_SCAN_MS = 2000

    def __init__(self, gpu_mode: str = "default"):
        super().__init__()
        self.settings = AppSettings.load()
        self._qt_translator: Optional[QtCore.QTranslator] = None
        self._apply_language()
        self.gpu_mode = gpu_mode
        self.ctrl = AcquisitionController(self.settings, self)
        self.files = FileActions(self.ctrl, self)
        self.marker_actions = MarkerActions(self.ctrl, self)
        self.marker_actions.status.connect(lambda text: self.statusBar().showMessage(text, 6000))
        self.cpu_monitor = CPUUsageMonitor()
        self._data_dirty = False
        self._page_index = PAGE_LIVE
        self._software_banner = False

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setMinimumSize(1100, 720)
        self._set_window_icon()

        self.ctrl.data_appended.connect(self._on_data_appended)
        self.ctrl.data_reset.connect(self._on_data_reset)
        self.ctrl.state_changed.connect(self._on_state_changed)
        self.ctrl.device_changed.connect(self._on_device_changed)
        self.ctrl.message.connect(self._on_message)
        self.ctrl.markers_changed.connect(self._on_markers_changed)
        self.ctrl.recording_changed.connect(self._update_recording_label)

        self._build_ui()
        self._setup_timers()
        self._setup_shortcuts()
        self._restore_geometry()

    # ------------------------------------------------------------- building

    def _set_window_icon(self) -> None:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
        path = base / "assets" / "icons" / "icon.png"
        if path.exists():
            self.setWindowIcon(QtGui.QIcon(str(path)))

    def _build_ui(self) -> None:
        self.theme = resolve_theme(self.settings.theme)
        apply_theme(QtWidgets.QApplication.instance(), self.theme)
        t = self.theme

        root = QtWidgets.QWidget()
        root.setObjectName("Root")
        h = QtWidgets.QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # navigation rail
        rail = QtWidgets.QFrame()
        rail.setObjectName("NavRail")
        rail.setFixedWidth(104)
        rl = QtWidgets.QVBoxLayout(rail)
        rl.setContentsMargins(8, 12, 8, 12)
        rl.setSpacing(6)
        logo = QtWidgets.QLabel()
        logo.setPixmap(icon("bolt", t.accent, 28).pixmap(28, 28))
        logo.setAlignment(QtCore.Qt.AlignCenter)
        logo.setToolTip(APP_NAME)
        rl.addWidget(logo)
        rl.addSpacing(10)
        self.nav_group = QtWidgets.QButtonGroup(rail)
        self.nav_group.setExclusive(True)
        self._nav_buttons = []
        pages = [("live", tr("Live")), ("analysis", tr("Analysis")), ("device", tr("Device")),
                 ("settings", tr("Settings")), ("info", tr("About"))]
        for idx, (ic, text) in enumerate(pages):
            b = QtWidgets.QToolButton()
            b.setObjectName("NavButton")
            b.setText(text)
            b.setCheckable(True)
            b.setToolButtonStyle(QtCore.Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QtCore.QSize(22, 22))
            b.setFixedSize(92, 60)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setToolTip(f"{text}  (Ctrl+{idx + 1})")
            self.nav_group.addButton(b, idx)
            self._nav_buttons.append((b, ic))
            if idx == PAGE_SETTINGS:
                rl.addStretch()
            rl.addWidget(b, 0, QtCore.Qt.AlignHCenter)
        self.nav_group.idClicked.connect(self._show_page)
        h.addWidget(rail)

        # right column
        right = QtWidgets.QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self._build_top_bar())

        self.banner_box = QtWidgets.QVBoxLayout()
        self.banner_box.setContentsMargins(16, 8, 16, 0)
        self.gpu_banner = Banner(tr("Software rendering active: GPU acceleration is unavailable on "
                                    "this system. Performance is reduced."), t.warning)
        self.gpu_banner.setVisible(self._software_banner)
        self.banner_box.addWidget(self.gpu_banner)
        right.addLayout(self.banner_box)

        self.stack = QtWidgets.QStackedWidget()
        self.live = LivePage(self.ctrl, t)
        self.analysis = AnalysisPage(self.ctrl, self.files, t)
        self.device = DevicePage(self.ctrl, t)
        self.settings_page = SettingsPage(self.settings)
        self.about = AboutPage(self.gpu_mode, t)
        for p in (self.live, self.analysis, self.device, self.settings_page, self.about):
            self.stack.addWidget(p)
        self.settings_page.changed.connect(self._on_setting_changed)
        self.live.plot.cursor_values.connect(self._on_cursor)
        self.live.plot.cursor_left.connect(lambda: self.cursor_label.setText(""))
        self.analysis.plot.cursor_values.connect(self._on_cursor)
        self.analysis.plot.cursor_left.connect(lambda: self.cursor_label.setText(""))
        for plot in (self.live.plot, self.analysis.plot):
            self.marker_actions.attach(plot)
        self.live.marker_btn.clicked.connect(self.marker_actions.add_now)
        self.analysis.del_markers_btn.clicked.connect(self.marker_actions.delete_all)
        right.addWidget(self.stack, 1)
        h.addLayout(right, 1)
        self.setCentralWidget(root)

        self._build_status_bar()
        self._refresh_nav_icons()
        self._show_page(self._page_index)
        self._refresh_ports()
        self._on_state_changed(self.ctrl.state)
        self._on_data_reset()
        self._on_markers_changed()
        self._update_recording_label()

    def _build_top_bar(self) -> QtWidgets.QWidget:
        t = self.theme
        bar = QtWidgets.QFrame()
        bar.setObjectName("TopBar")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(20, 10, 16, 10)
        lay.setSpacing(8)
        self.page_title = label("", "PageTitle")
        lay.addWidget(self.page_title)
        lay.addSpacing(12)
        self.status_pill = QtWidgets.QFrame()
        self.status_pill.setObjectName("StatusPill")
        pill = QtWidgets.QHBoxLayout(self.status_pill)
        pill.setContentsMargins(10, 3, 12, 3)
        pill.setSpacing(7)
        self.status_dot = QtWidgets.QLabel()
        self.status_dot.setFixedSize(8, 8)
        pill.addWidget(self.status_dot)
        self.status_text = label("")
        pill.addWidget(self.status_text)
        lay.addWidget(self.status_pill)
        lay.addStretch()

        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(260)
        self.port_combo.setToolTip(tr("Serial port of the meter"))
        lay.addWidget(self.port_combo)
        self.refresh_btn = QtWidgets.QToolButton()
        self.refresh_btn.setObjectName("Chip")
        self.refresh_btn.setIcon(icon("refresh", t.text_secondary, 16))
        self.refresh_btn.setToolTip(tr("Refresh the port list"))
        self.refresh_btn.clicked.connect(self._refresh_ports)
        lay.addWidget(self.refresh_btn)
        self.show_all_ports = QtWidgets.QToolButton()
        self.show_all_ports.setObjectName("Chip")
        self.show_all_ports.setText(tr("All ports"))
        self.show_all_ports.setCheckable(True)
        self.show_all_ports.setToolTip(tr("Also list ports that do not look like USB serial devices"))
        self.show_all_ports.toggled.connect(self._refresh_ports)
        lay.addWidget(self.show_all_ports)
        lay.addSpacing(8)

        self.start_btn = button(tr("Start"), "success", "play", "#ffffff")
        self.start_btn.setMinimumWidth(110)
        self.start_btn.setToolTip(tr("Start / stop the acquisition (Ctrl+R)"))
        self.start_btn.clicked.connect(self.toggle_acquisition)
        lay.addWidget(self.start_btn)
        self.clear_btn = button(tr("Clear"), "", "trash", t.text_secondary)
        self.clear_btn.setToolTip(tr("Delete the recorded data"))
        self.clear_btn.clicked.connect(self._clear)
        lay.addWidget(self.clear_btn)
        return bar

    def _build_status_bar(self) -> None:
        sb = QtWidgets.QStatusBar()
        self.setStatusBar(sb)
        self.cursor_label = label("")
        self.cursor_label.setStyleSheet("font-family: monospace;")
        sb.addPermanentWidget(self.cursor_label)
        self.rec_button = QtWidgets.QToolButton()
        self.rec_button.setAutoRaise(True)
        self.rec_button.setCursor(QtCore.Qt.PointingHandCursor)
        self.rec_button.clicked.connect(self.ctrl.open_recordings_folder)
        sb.addPermanentWidget(self.rec_button)
        self.samples_label = label("")
        sb.addPermanentWidget(self.samples_label)
        self.memory_label = label("")
        self.memory_label.setToolTip(tr("Memory used by the recorded data"))
        sb.addPermanentWidget(self.memory_label)
        self.cpu_bar = CPUBar(bar_count=6)
        self.cpu_bar.setFixedHeight(16)
        self.cpu_bar.set_colors(self.theme.accent, self.theme.border)
        self.cpu_pct = label("--%")
        sb.addPermanentWidget(self.cpu_bar)
        sb.addPermanentWidget(self.cpu_pct)
        show = self.settings.show_cpu_usage
        self.cpu_bar.setVisible(show)
        self.cpu_pct.setVisible(show)

    def _refresh_nav_icons(self) -> None:
        for idx, (b, ic) in enumerate(self._nav_buttons):
            color = self.theme.accent if idx == self._page_index else self.theme.text_secondary
            b.setIcon(icon(ic, color, 22))

    def _setup_timers(self) -> None:
        self.frame_timer = QtCore.QTimer(self)
        self.frame_timer.timeout.connect(self._on_frame)
        self.frame_timer.start(int(1000 / self.settings.plot_fps))
        self.metrics_timer = QtCore.QTimer(self)
        self.metrics_timer.timeout.connect(self._on_metrics)
        self.metrics_timer.start(self.METRICS_INTERVAL_MS)
        self.slow_timer = QtCore.QTimer(self)
        self.slow_timer.timeout.connect(self._on_slow_tick)
        self.slow_timer.start(self.SLOW_INTERVAL_MS)
        self.port_timer = QtCore.QTimer(self)
        self.port_timer.timeout.connect(self._scan_ports)
        self.port_timer.start(self.PORT_SCAN_MS)

    def _setup_shortcuts(self) -> None:
        def sc(seq, fn):
            QtGui.QShortcut(QtGui.QKeySequence(seq), self, activated=fn)
        sc("Ctrl+R", self.toggle_acquisition)
        sc("M", self.marker_actions.add_now)
        sc("Ctrl+O", lambda: self.files.import_csv())
        sc("Ctrl+E", lambda: self.files.export_csv(*self._export_target()))
        sc("Ctrl+P", lambda: self.files.export_pdf(*self._export_target()))
        for i in range(5):
            sc(f"Ctrl+{i + 1}", lambda i=i: self._show_page(i))

    def _apply_language(self) -> None:
        """Select our catalog and Qt's own translations (standard dialogs,
        e.g. the file dialog's labels and buttons)."""
        lang = set_language(self.settings.language)
        app = QtWidgets.QApplication.instance()
        if self._qt_translator is not None:
            app.removeTranslator(self._qt_translator)
            self._qt_translator.deleteLater()
            self._qt_translator = None
        if lang != "en":
            tr_ = QtCore.QTranslator(self)
            path = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.TranslationsPath)
            if tr_.load(f"qtbase_{lang}", path):
                app.installTranslator(tr_)
                self._qt_translator = tr_

    def _export_target(self):
        # The selection only means something on the Analysis page; from any
        # other page the shortcuts export the whole recording.
        if self._page_index == PAGE_ANALYSIS:
            return self.analysis.export_target()
        return self.ctrl.data(), "all"

    def rebuild_ui(self) -> None:
        """Recreate every widget (language or theme change); data is kept."""
        old = self.centralWidget()
        if old is not None:
            # Pages connected to controller signals must be disconnected,
            # otherwise the deleted widgets keep receiving them.
            self.device.detach()
        self._build_ui()
        if old is not None:
            old.deleteLater()

    def show_software_rendering_banner(self) -> None:
        self._software_banner = True
        self.gpu_banner.show()

    # -------------------------------------------------------------- pages

    def _show_page(self, idx: int) -> None:
        self._page_index = idx
        self.stack.setCurrentIndex(idx)
        b = self.nav_group.button(idx)
        if b is not None:
            b.setChecked(True)
        self.page_title.setText(self._nav_buttons[idx][0].text())
        self._refresh_nav_icons()
        if idx == PAGE_LIVE:
            self.live.refresh()
        elif idx == PAGE_ANALYSIS:
            self.analysis.apply_settings(self.settings)
            self.analysis.refresh_live(force=True)
        elif idx == PAGE_DEVICE:
            self.device.update_status()

    # -------------------------------------------------------- acquisition

    def toggle_acquisition(self) -> None:
        if self.ctrl.is_running:
            self.ctrl.stop()
            return
        port = self.port_combo.currentData()
        if not port:
            QtWidgets.QMessageBox.information(self, tr("Start"), tr("Select a serial port first."))
            return
        if not self.files.confirm_discard():
            return
        self.ctrl.start(port)
        if self._page_index not in (PAGE_LIVE, PAGE_DEVICE):
            self._show_page(PAGE_LIVE)

    def _clear(self) -> None:
        if not self.files.confirm_discard():
            return
        self.ctrl.clear()

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentData() or self.ctrl.port
        ports = PortDiscovery.get_ports(self.show_all_ports.isChecked())
        self._port_signature = tuple(p for p, _ in ports)
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        for device, text in ports:
            self.port_combo.addItem(text, device)
        if not ports:
            self.port_combo.addItem(tr("No device found"), None)
        idx = self.port_combo.findData(current)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)
        self.port_combo.blockSignals(False)

    def _scan_ports(self) -> None:
        # Cheap periodic rescan while idle so plugging the meter in shows up
        # without clicking Refresh. Never starts an acquisition by itself.
        if self.ctrl.is_running or self.port_combo.view().isVisible():
            return
        ports = tuple(p for p, _ in PortDiscovery.get_ports(self.show_all_ports.isChecked()))
        if ports != getattr(self, "_port_signature", None):
            self._refresh_ports()

    # ------------------------------------------------------------ signals

    def _on_data_appended(self) -> None:
        self._data_dirty = True

    def _on_data_reset(self) -> None:
        self._data_dirty = False
        self.live.on_data_reset()
        self.analysis.on_data_reset()
        self._update_status_labels()

    def _on_frame(self) -> None:
        if self._data_dirty and self._page_index == PAGE_LIVE:
            self._data_dirty = False
            self.live.refresh()

    def _on_metrics(self) -> None:
        if self._page_index == PAGE_LIVE:
            self.live.update_metrics()

    def _on_slow_tick(self) -> None:
        self._update_status_labels()
        if self.ctrl.is_recording_to_disk:
            self._update_recording_label()
        if self._page_index == PAGE_ANALYSIS and self.ctrl.is_running:
            self.analysis.refresh_live()
        elif self._page_index == PAGE_DEVICE:
            self.device.update_status()
        if self.settings.show_cpu_usage:
            usage = self.cpu_monitor.get_usage()
            if usage is not None:
                self.cpu_bar.set_usage(usage)
                self.cpu_pct.setText(f"CPU {usage:.0f}%")
        if self.ctrl.state == STATE_STREAMING:
            self._on_state_changed(STATE_STREAMING)   # refresh the rate in the pill

    def _on_markers_changed(self) -> None:
        markers = self.ctrl.markers.sorted()
        self.live.plot.set_markers(markers)
        self.analysis.on_markers_changed()

    def _update_recording_label(self) -> None:
        c = self.ctrl
        path = c.recording_path
        if path is None:
            self.rec_button.setVisible(False)
            return
        self.rec_button.setVisible(True)
        size = c.writer.size_bytes / 1e6 if c.writer is not None else 0.0
        if c.is_recording_to_disk:
            self.rec_button.setText(tr("Recording to {file} ({size})", file=path.name,
                                       size=f"{size:.1f} MB"))
            self.rec_button.setStyleSheet(f"color: {self.theme.danger};")
        else:
            self.rec_button.setText(tr("Saved: {file}", file=path.name))
            self.rec_button.setStyleSheet(f"color: {self.theme.text_secondary};")
        self.rec_button.setToolTip(tr("{path}\nClick to open the folder.", path=str(path)))

    def _update_status_labels(self) -> None:
        n = len(self.ctrl.store)
        self.samples_label.setText(tr("{n} samples", n=f"{n:,}"))
        mb = self.ctrl.store.nbytes / 1e6
        self.memory_label.setText(f"{mb:.1f} MB")

    def _on_state_changed(self, state: str) -> None:
        t = self.theme
        self._update_status_labels()
        running = self.ctrl.is_running
        if running:
            self.start_btn.setText(tr("Stop"))
            self.start_btn.setIcon(icon("stop", "#ffffff", 18))
            set_variant(self.start_btn, "danger")
        else:
            self.start_btn.setText(tr("Start"))
            self.start_btn.setIcon(icon("play", "#ffffff", 18))
            set_variant(self.start_btn, "success")
        self.port_combo.setEnabled(not running)
        self.refresh_btn.setEnabled(not running)
        self.show_all_ports.setEnabled(not running)

        rate = self.ctrl.recent_rate()
        text, color = {
            STATE_IDLE: (tr("Disconnected"), t.text_muted),
            STATE_CONNECTING: (tr("Connecting..."), t.warning),
            STATE_STREAMING: (tr("Live · {rate}", rate=f"{rate:.0f} Hz"), t.success),
            STATE_STALE: (tr("No data"), t.warning),
            STATE_RECONNECTING: (tr("Reconnecting..."), t.warning),
        }.get(state, (state, t.text_muted))
        if state == STATE_IDLE and self.ctrl.source_name and len(self.ctrl.store):
            text = tr("Stopped") if self.ctrl.source == "live" else tr("File loaded")
        self.status_text.setText(text)
        self.status_text.setStyleSheet(f"color: {color}; font-weight: 600; font-size: 12px;")
        self.status_dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        self.status_pill.setStyleSheet(
            f"QFrame#StatusPill {{ background: {t.surface_alt}; border: 1px solid {t.border}; "
            f"border-radius: 12px; }}")
        if self._page_index == PAGE_DEVICE:
            self.device.update_status()
        elif self._page_index == PAGE_ANALYSIS and state == STATE_IDLE:
            self.analysis.refresh_live(force=True)

    def _on_device_changed(self) -> None:
        if self._page_index == PAGE_DEVICE:
            self.device.update_status()

    def _on_message(self, level: str, text: str) -> None:
        if level == "error":
            self.statusBar().showMessage(text, 15000)
            QtWidgets.QMessageBox.warning(self, APP_NAME, text)
        else:
            self.statusBar().showMessage(text, 10000 if level == "warning" else 6000)

    def _on_cursor(self, t: float, v: float, i: float, p: float) -> None:
        from ..export.units import format_si
        self.cursor_label.setText(
            f"t {t:.4f} s   V {format_si(v, 'V')}   I {format_si(i, 'A')}   P {format_si(p, 'W')}")

    def _on_setting_changed(self, name: str) -> None:
        s = self.settings
        if name in _REBUILD_KEYS:
            self._apply_language()
            idx = self._page_index
            self.rebuild_ui()
            self._show_page(idx)
            if name != "*":
                return
            # Restore defaults: also apply what the rebuild does not cover.
            self.frame_timer.start(int(1000 / s.plot_fps))
            self.ctrl.apply_calibration()
            self.cpu_bar.setVisible(s.show_cpu_usage)
            self.cpu_pct.setVisible(s.show_cpu_usage)
            return
        if name == "plot_fps":
            self.frame_timer.start(int(1000 / s.plot_fps))
        if name == "show_cpu_usage":
            self.cpu_bar.setVisible(s.show_cpu_usage)
            self.cpu_pct.setVisible(s.show_cpu_usage)
        if name == "time_window_s":
            self.live.plot.set_window_seconds(s.time_window_s)
            self.live._sync_window_combo(s.time_window_s)
        if name in ("show_grid", "grid_alpha", "show_crosshair", "line_width", "antialias",
                    "unit_mode", "show_voltage", "show_current", "show_power"):
            self.live.apply_settings(s)
            self.analysis.apply_settings(s)
        if name in ("unit_mode", "value_decimals", "avg_power_mode", "avg_window_s"):
            self.live.update_metrics()
        if name == "spectrum_signal":
            self.analysis.sync_spectrum_signal()

    # ------------------------------------------------------------ window

    def _restore_geometry(self) -> None:
        qs = QtCore.QSettings("EdgePowerMeter", "EdgePowerMeter")
        geo = qs.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1440, 900)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.files.busy:
            QtWidgets.QMessageBox.information(self, APP_NAME, tr("Please wait for the export to finish."))
            event.ignore()
            return
        if self.ctrl.has_unsaved_data and self.settings.confirm_discard and len(self.ctrl.store):
            if not confirm(self, tr("Unsaved data"),
                           tr("The recording has not been exported. Quit anyway?"),
                           tr("Quit"), tr("Cancel")):
                event.ignore()
                return
        QtCore.QSettings("EdgePowerMeter", "EdgePowerMeter").setValue(
            "window/geometry", self.saveGeometry())
        self.ctrl.shutdown()
        self.settings.save()
        event.accept()
