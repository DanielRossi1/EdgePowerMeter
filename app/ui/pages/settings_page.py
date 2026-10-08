"""Settings page: every option applies immediately and is saved automatically."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...core import AppSettings
from ...i18n import LANGUAGES, tr
from ...serial import SerialConfig
from ..widgets.common import Card, FormGrid, button, combo, confirm, scroll_page, spin


class SettingsPage(QtWidgets.QWidget):
    # attribute name of the setting that changed ("*" = everything)
    changed = QtCore.Signal(str)

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
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
        f.add(tr("Report title"), title)
        notes = QtWidgets.QPlainTextEdit(s.report_notes)
        notes.setPlaceholderText(tr("Optional notes printed under the title (device under test, model, conditions...)"))
        notes.setFixedHeight(70)
        notes.textChanged.connect(lambda: self._update("report_notes", notes.toPlainText()))
        f.add(tr("Report notes"), notes)
        card.body.addLayout(f)
        col.addWidget(card)

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
        self.changed.emit(name)

    def _combo(self, name: str, items) -> QtWidgets.QComboBox:
        c = combo(items, getattr(self.settings, name))
        c.currentIndexChanged.connect(lambda _: self._update(name, c.currentData()))
        return c

    def _check(self, name: str, text: str) -> QtWidgets.QCheckBox:
        c = QtWidgets.QCheckBox(text)
        c.setChecked(bool(getattr(self.settings, name)))
        c.toggled.connect(lambda on: self._update(name, on))
        return c

    def _spin(self, name: str, lo, hi, decimals, step, suffix=""):
        sp = spin(lo, hi, getattr(self.settings, name), decimals, step, suffix)
        cast = int if decimals == 0 else float
        sp.valueChanged.connect(lambda v: self._update(name, cast(v)))
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
        keep = {"last_dir": self.settings.last_dir}
        fresh = AppSettings(**keep)
        for k, v in fresh.as_dict().items():
            setattr(self.settings, k, v)
        self.settings.save()
        self.changed.emit("*")
