"""Settings page: every option applies immediately and is saved automatically."""

from __future__ import annotations

import json
from typing import Dict

from PySide6 import QtCore, QtWidgets

from ...core import AppSettings
from ...i18n import LANGUAGES, tr
from ...serial import SerialConfig
from ..widgets.common import Card, FormGrid, button, combo, confirm, label, scroll_page, spin

# Snippets copied by "Copy MCP config" (the MCP bridge is `edgepowermeter --mcp`).
MCP_ADD_COMMAND = "claude mcp add edgepowermeter -- edgepowermeter --mcp"
MCP_CONFIG_JSON = json.dumps(
    {"mcpServers": {"edgepowermeter": {"command": "edgepowermeter", "args": ["--mcp"]}}},
    indent=2)


def _show(widget, value) -> None:
    """Set a bound widget to `value` without emitting its change signal."""
    widget.blockSignals(True)
    try:
        if isinstance(widget, QtWidgets.QComboBox):
            idx = widget.findData(value)
            if idx >= 0:
                widget.setCurrentIndex(idx)
        elif isinstance(widget, QtWidgets.QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QtWidgets.QAbstractSpinBox):
            widget.setValue(value)
        elif isinstance(widget, QtWidgets.QPlainTextEdit):
            if widget.toPlainText() != value:
                widget.setPlainText(value)
        elif isinstance(widget, QtWidgets.QLineEdit):
            if widget.text() != value:
                widget.setText(value)
    finally:
        widget.blockSignals(False)


class SettingsPage(QtWidgets.QWidget):
    # attribute name of the setting that changed ("*" = everything)
    changed = QtCore.Signal(str)

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._bound: Dict[str, QtWidgets.QWidget] = {}   # setting name -> its widget
        s = settings
        body = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        # -------------------------------------------------------- general
        card = Card(tr("General"))
        f = FormGrid()
        langs = [(tr("Automatic (system language)"), "auto")] + [(n, c) for c, n in LANGUAGES.items()]
        f.add(tr("Language"), self._combo("language", langs),
              tr("Translations are generated offline by a local machine-translation model "
                 "and reviewed for technical terms."))
        f.add(tr("Theme"), self._combo("theme", [(tr("Dark"), "dark"), (tr("Light"), "light"),
                                                 (tr("Follow system"), "system")]))
        f.add_full(self._check("confirm_discard", tr("Ask before discarding unsaved data")))
        card.body.addLayout(f)
        col.addWidget(card)

        # -------------------------------------------------------- display
        card = Card(tr("Display"))
        f = FormGrid()
        f.add(tr("Plot refresh rate"), self._spin("plot_fps", 5, 120, 0, 5, " FPS"),
              tr("Lower values reduce CPU usage; data is never lost, only drawn less often."))
        f.add(tr("Default time window"), self._spin("time_window_s", 0.5, 3600, 1, 1, " s"))
        f.add(tr("Line width"), self._spin("line_width", 0.5, 5, 1, 0.5, " px"))
        f.add_full(self._check("antialias", tr("Smooth lines (antialiasing)")))
        f.add_full(self._check("show_grid", tr("Show grid")))
        f.add(tr("Grid opacity"), self._spin("grid_alpha", 0, 1, 2, 0.05))
        f.add_full(self._check("show_crosshair", tr("Show cursor values when hovering the plot")))
        f.add(tr("Units"), self._combo("unit_mode", [
            (tr("Automatic (mA, µA, ...)"), "auto"), (tr("Base units (V, A, W)"), "base"),
            (tr("Milli units (mV, mA, mW)"), "milli")]))
        f.add(tr("Significant digits"), self._spin("value_decimals", 1, 6, 0, 1))
        f.add(tr("Average power"), self._combo("avg_power_mode", [
            (tr("Moving window"), "window"), (tr("Whole recording"), "session")]))
        f.add(tr("Moving window length"), self._spin("avg_window_s", 0.05, 600, 2, 0.5, " s"))
        f.add_full(self._check("show_cpu_usage", tr("Show CPU usage in the status bar")))
        f.add_full(self._check("use_opengl", tr("Hardware acceleration (OpenGL)")),
                   tr("Takes effect after restarting the application."))
        card.body.addLayout(f)
        col.addWidget(card)

        # ---------------------------------------------------- acquisition
        card = Card(tr("Acquisition"))
        f = FormGrid()
        f.add(tr("Baud rate"), self._combo("baud_rate", [(f"{b:,}", b) for b in SerialConfig.BAUD_CHOICES]),
              tr("Irrelevant for the native USB port of the ESP32-C3; kept for USB-UART adapters."))
        f.add_full(self._check("auto_reconnect", tr("Reconnect automatically if the device is unplugged")),
                   tr("The recording continues where it stopped. A manual Stop never reconnects."))
        f.add(tr("No-data timeout"), self._spin("no_data_timeout_s", 3, 600, 0, 1, " s"),
              tr("The acquisition stops with an error if the device stays silent this long."))
        f.add(tr("Host averaging"), self._spin("host_averaging", 1, 1000, 0, 1, " " + tr("samples")),
              tr("Averages consecutive samples in the application to reduce noise and memory. "
                 "Prefer the device averaging (Device page) when available."))
        card.body.addLayout(f)
        col.addWidget(card)

        # ------------------------------------------------------ recording
        card = Card(tr("Recording"),
                    tr("Each acquisition is written while it runs to one CSV file (same format "
                       "as Export CSV, markers included). Nothing is lost if the PC or the app "
                       "stops, and no hidden or temporary files are created. Recordings shorter "
                       "than one second are removed automatically."))
        f = FormGrid()
        f.add_full(self._check("autosave", tr("Save recordings automatically")))
        folder_row = QtWidgets.QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        from ...export.recorder import default_folder
        self.folder_edit = QtWidgets.QLineEdit(s.recordings_dir)
        self.folder_edit.setPlaceholderText(str(default_folder()))
        self.folder_edit.editingFinished.connect(
            lambda: self._update("recordings_dir", self.folder_edit.text().strip()))
        folder_row.addWidget(self.folder_edit, 1)
        self._bound["recordings_dir"] = self.folder_edit
        browse = button(tr("Choose..."))
        browse.clicked.connect(self._choose_folder)
        folder_row.addWidget(browse)
        holder = QtWidgets.QWidget()
        holder.setLayout(folder_row)
        f.add(tr("Folder"), holder,
              tr("About 60 bytes per sample: roughly 200 MB per hour at the default ~900 Hz."))
        card.body.addLayout(f)
        col.addWidget(card)

        # --------------------------------------------------------- export
        card = Card(tr("Export"))
        f = FormGrid()
        f.add(tr("CSV separator"), self._combo("csv_separator", [
            (tr("Comma ( , )"), ","), (tr("Semicolon ( ; )"), ";"), (tr("Tab"), "\t")]))
        f.add(tr("Decimal separator"), self._combo("csv_decimal", [
            (tr("Point ( 1.5 )"), "."), (tr("Comma ( 1,5 )"), ",")]),
              tr("Use comma for spreadsheets in locales such as Italian or German "
                 "(the separator then becomes a semicolon)."))
        f.add(tr("Time column"), self._combo("csv_time_format", [
            (tr("Date and time (local)"), "datetime"), (tr("Unix time (seconds)"), "epoch")]))
        f.add_full(self._check("pdf_include_graphs", tr("PDF: include graphs")))
        f.add_full(self._check("pdf_include_psu", tr("PDF: include power supply quality")))
        f.add_full(self._check("pdf_include_spectrum", tr("PDF: include frequency spectrum")))
        f.add(tr("Spectrum signal"), self._combo("spectrum_signal", [
            (tr("Current"), "current"), (tr("Power"), "power"), (tr("Voltage"), "voltage")]))
        title = QtWidgets.QLineEdit(s.report_title)
        title.setPlaceholderText(tr("Power measurement report"))
        title.editingFinished.connect(lambda: self._update("report_title", title.text()))
        self._bound["report_title"] = title
        f.add(tr("Report title"), title)
        notes = QtWidgets.QPlainTextEdit(s.report_notes)
        notes.setPlaceholderText(tr("Optional notes printed under the title (device under test, model, conditions...)"))
        notes.setFixedHeight(70)
        notes.textChanged.connect(lambda: self._update("report_notes", notes.toPlainText()))
        self._bound["report_notes"] = notes
        f.add(tr("Report notes"), notes)
        card.body.addLayout(f)
        col.addWidget(card)

        # ------------------------------------------------ automation API
        card = Card(tr("Automation API (AI agents)"),
                    tr("Lets AI agents and scripts read the meter and control it (start, mark, "
                       "measure, export). Only this computer can connect, and every request needs "
                       "the token below."))
        f = FormGrid()
        f.add_full(self._check("api_enabled", tr("Enable the automation API")),
                   tr("Takes effect immediately. It is also available for one session with "
                      "the --api option."))
        f.add(tr("Port"), self._spin("api_port", 1024, 65535, 0, 1, on_finish=True),
              tr("TCP port on 127.0.0.1 (localhost only)."))
        self.api_status_label = label("")
        f.add(tr("Status"), self.api_status_label)
        self.api_url_label = label("")
        self.api_url_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        f.add(tr("Address"), self.api_url_label)
        token_row = QtWidgets.QHBoxLayout()
        token_row.setContentsMargins(0, 0, 0, 0)
        self.api_token_edit = QtWidgets.QLineEdit()
        self.api_token_edit.setReadOnly(True)
        self.api_token_edit.setMinimumWidth(240)
        token_row.addWidget(self.api_token_edit, 1)
        copy_token = button(tr("Copy"))
        copy_token.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(self.settings.api_token))
        token_row.addWidget(copy_token)
        regen = button(tr("Regenerate"), "flat")
        regen.clicked.connect(self._regenerate_token)
        token_row.addWidget(regen)
        holder = QtWidgets.QWidget()
        holder.setLayout(token_row)
        f.add(tr("Token"), holder,
              tr("Clients send it as 'Authorization: Bearer <token>'. It is also written to the "
                 "discovery file, which only your user can read."))
        mcp_btn = button(tr("Copy MCP config"))
        mcp_btn.clicked.connect(self._copy_mcp_config)
        f.add(tr("MCP server"), mcp_btn,
              tr("Copies the setup for Claude Code and for MCP clients that use an mcpServers "
                 "file. Claude Code: run the copied 'claude mcp add' command once."))
        f.add_full(self._mono_label(MCP_ADD_COMMAND))
        card.body.addLayout(f)
        col.addWidget(card)
        self._sync_api()

        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        reset = button(tr("Restore defaults"), "flat")
        reset.clicked.connect(self._reset)
        row.addWidget(reset)
        col.addLayout(row)
        col.addStretch()

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll_page(body, 820))

    # ----------------------------------------------------------- binders

    def _update(self, name: str, value) -> None:
        if getattr(self.settings, name) == value:
            return
        setattr(self.settings, name, value)
        self.settings.validate()
        self.settings.save()
        if name.startswith("api_"):
            self._sync_api()
        self.changed.emit(name)

    def refresh_from_settings(self) -> None:
        """Show the current settings after they changed elsewhere (e.g. through the API)."""
        for name, widget in self._bound.items():
            _show(widget, getattr(self.settings, name))
        self._sync_api()

    def _sync_api(self) -> None:
        self.api_token_edit.setText(self.settings.api_token)

    def set_api_status(self, text: str, color: str, tooltip: str = "", url: str = "") -> None:
        """Show whether the API server is running and where (set by the main window).

        `url` is the address actually in use: it can differ from the saved
        port when the app was started with --api-port.
        """
        self.api_status_label.setText(text)
        self.api_status_label.setStyleSheet(f"color: {color};")
        self.api_status_label.setToolTip(tooltip)
        if url:
            self.api_url_label.setText(url)

    def _regenerate_token(self) -> None:
        if not confirm(self, tr("Regenerate token"),
                       tr("Programs using the current token will be disconnected. Continue?"),
                       tr("Regenerate"), tr("Cancel")):
            return
        from ...api.schema import generate_token
        self._update("api_token", generate_token())

    def _copy_mcp_config(self) -> None:
        QtWidgets.QApplication.clipboard().setText(MCP_ADD_COMMAND + "\n\n" + MCP_CONFIG_JSON)

    @staticmethod
    def _mono_label(text: str) -> QtWidgets.QLabel:
        lab = label(text)
        lab.setStyleSheet("font-family: monospace;")
        lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        return lab

    def _combo(self, name: str, items) -> QtWidgets.QComboBox:
        c = combo(items, getattr(self.settings, name))
        c.currentIndexChanged.connect(lambda _: self._update(name, c.currentData()))
        self._bound[name] = c
        return c

    def _check(self, name: str, text: str) -> QtWidgets.QCheckBox:
        c = QtWidgets.QCheckBox(text)
        c.setChecked(bool(getattr(self.settings, name)))
        c.toggled.connect(lambda on: self._update(name, on))
        self._bound[name] = c
        return c

    def _spin(self, name: str, lo, hi, decimals, step, suffix="", on_finish: bool = False):
        """on_finish: apply on editingFinished (Enter or focus-out) instead of every step.
        Needed for settings whose change has side effects, such as restarting the API."""
        sp = spin(lo, hi, getattr(self.settings, name), decimals, step, suffix)
        cast = int if decimals == 0 else float
        if on_finish:
            sp.editingFinished.connect(lambda: self._update(name, cast(sp.value())))
        else:
            sp.valueChanged.connect(lambda v: self._update(name, cast(v)))
        self._bound[name] = sp
        return sp

    def _choose_folder(self) -> None:
        from ...export.recorder import default_folder
        start = self.folder_edit.text().strip() or str(default_folder())
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, tr("Recordings folder"), start, QtWidgets.QFileDialog.DontUseNativeDialog)
        if path:
            self.folder_edit.setText(path)
            self._update("recordings_dir", path)

    def _reset(self) -> None:
        if not confirm(self, tr("Restore defaults"),
                       tr("Reset all settings (including calibration) to their defaults?"),
                       tr("Reset"), tr("Cancel")):
            return
        # The API token stays: a reset must not lock out clients that already use it.
        keep = {"last_dir": self.settings.last_dir, "api_token": self.settings.api_token}
        fresh = AppSettings(**keep)
        for k, v in fresh.as_dict().items():
            setattr(self.settings, k, v)
        self.settings.save()
        self.changed.emit("*")
