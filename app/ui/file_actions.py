"""Import / export actions with file dialogs and a background worker."""

from __future__ import annotations

import copy
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6 import QtCore, QtWidgets

from ..core import Samples
from ..export import ReportOptions, export_csv, export_pdf, format_duration, format_si
from ..export.csv_io import import_recording
from ..export.recorder import user_home
from ..i18n import tr
from .controller import AcquisitionController


# The native (GTK/portal) dialog follows the desktop theme, not the app's,
# so it would stay light under the dark theme; Qt's own dialog is styled.
_DIALOG_OPTIONS = QtWidgets.QFileDialog.DontUseNativeDialog


class _Worker(QtCore.QThread):
    progress = QtCore.Signal(float)
    done = QtCore.Signal(object, str)   # result, error message ("" on success)

    def __init__(self, fn: Callable[[Callable[[float], None]], object]):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn(self.progress.emit)
            self.done.emit(result, "")
        except Exception as e:  # reported to the user
            self.done.emit(None, str(e) or e.__class__.__name__)


class FileActions(QtCore.QObject):
    """Owns running jobs so they are not garbage collected mid-flight."""

    busy_changed = QtCore.Signal(bool)

    def __init__(self, ctrl: AcquisitionController, parent_widget: QtWidgets.QWidget):
        super().__init__(parent_widget)
        self.ctrl = ctrl
        self.parent_widget = parent_widget
        self._jobs = set()

    @property
    def busy(self) -> bool:
        return bool(self._jobs)

    # ------------------------------------------------------------- helpers

    def _start_dir(self) -> str:
        d = self.ctrl.settings.last_dir
        if d and os.path.isdir(d):
            return d
        rec = self.ctrl.recordings_folder()
        return str(rec) if rec.is_dir() else str(user_home())

    def _remember_dir(self, path: str) -> None:
        self.ctrl.settings.last_dir = str(Path(path).parent)
        self.ctrl.settings.save()

    def _run(self, title: str, fn, on_success: Callable[[object], None]) -> None:
        dlg = QtWidgets.QProgressDialog(title, "", 0, 100, self.parent_widget)
        dlg.setWindowTitle(tr("Please wait"))
        dlg.setCancelButton(None)
        dlg.setWindowModality(QtCore.Qt.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.setAutoClose(False)
        worker = _Worker(fn)
        self._jobs.add(worker)
        self.busy_changed.emit(True)

        def finished(result, error: str) -> None:
            dlg.close()
            dlg.deleteLater()
            self._jobs.discard(worker)
            worker.deleteLater()
            self.busy_changed.emit(self.busy)
            if error:
                QtWidgets.QMessageBox.critical(self.parent_widget, tr("Error"), error)
            else:
                on_success(result)

        worker.progress.connect(lambda x: dlg.setValue(int(x * 100)))
        worker.done.connect(finished)
        worker.start()

    # ------------------------------------------------------------- actions

    def import_csv(self) -> None:
        if self.ctrl.is_running:
            QtWidgets.QMessageBox.information(self.parent_widget, tr("Import"),
                                              tr("Stop acquisition before importing data."))
            return
        if not self._confirm_discard():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self.parent_widget, tr("Import CSV"), self._start_dir(),
            tr("CSV files (*.csv *.txt);;All files (*)"), options=_DIALOG_OPTIONS)
        if not path:
            return
        self._remember_dir(path)

        def done(result) -> None:
            samples, markers = result
            self.ctrl.load(samples, Path(path).name, markers)
            text = tr("{n} samples imported ({duration}) from {file}.",
                      n=f"{len(samples):,}", duration=format_duration(samples.duration),
                      file=Path(path).name)
            if len(markers):
                text += "\n" + tr("{n} markers", n=len(markers))
            QtWidgets.QMessageBox.information(self.parent_widget, tr("Import completed"), text)

        self._run(tr("Reading {file}...", file=Path(path).name),
                  lambda progress: import_recording(Path(path), progress), done)

    def _mark_saved_if_unchanged(self, n: int) -> None:
        # Samples recorded while the export ran are not in the file.
        if len(self.ctrl.store) == n:
            self.ctrl.mark_saved()

    def _already_saved(self) -> bool:
        """Whole live recording already on disk: offer the file instead of a
        duplicate. Returns True when the caller must not export."""
        path = self.ctrl.recording_path
        if path is None or self.ctrl.is_recording_to_disk is None:
            return False
        box = QtWidgets.QMessageBox(self.parent_widget)
        box.setIcon(QtWidgets.QMessageBox.Information)
        box.setWindowTitle(tr("Export CSV"))
        box.setText(tr("This recording is already saved as CSV:\n{path}", path=str(path)))
        box.setInformativeText(tr("Markers are included in that file. Save a copy anyway?"))
        open_btn = box.addButton(tr("Open folder"), QtWidgets.QMessageBox.AcceptRole)
        copy_btn = box.addButton(tr("Save a copy"), QtWidgets.QMessageBox.ActionRole)
        box.addButton(tr("Cancel"), QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(open_btn)
        box.exec()
        if box.clickedButton() is open_btn:
            self.ctrl.open_recordings_folder()
            return True
        return box.clickedButton() is not copy_btn

    def export_csv(self, samples: Samples, scope: str) -> None:
        if scope == "all" and self._already_saved():
            return
        if not len(samples):
            QtWidgets.QMessageBox.information(self.parent_widget, tr("Export"), tr("No data to export."))
            return
        default = os.path.join(self._start_dir(),
                               f"measurement_{datetime.now():%Y%m%d_%H%M%S}.csv")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self.parent_widget, tr("Export CSV"), default, tr("CSV files (*.csv)"),
            options=_DIALOG_OPTIONS)
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        self._remember_dir(path)
        s = self.ctrl.settings
        samples = samples.copy()   # the store may be cleared/reused meanwhile
        markers = copy.deepcopy(self.ctrl.markers)

        def done(_):
            if scope == "all":
                self._mark_saved_if_unchanged(len(samples))
            self._notify_saved(path, len(samples))

        self._run(tr("Exporting CSV..."),
                  lambda progress: export_csv(Path(path), samples, s.csv_separator, s.csv_decimal,
                                              s.csv_time_format, progress, markers), done)

    def export_pdf(self, samples: Samples, scope: str) -> None:
        if len(samples) < 2:
            QtWidgets.QMessageBox.information(self.parent_widget, tr("Export"),
                                              tr("At least 2 samples are required."))
            return
        default = os.path.join(self._start_dir(), f"report_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self.parent_widget, tr("Export PDF report"), default, tr("PDF files (*.pdf)"),
            options=_DIALOG_OPTIONS)
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self._remember_dir(path)
        s = self.ctrl.settings
        opts = ReportOptions(
            title=s.report_title,
            notes=s.report_notes,
            include_graphs=s.pdf_include_graphs,
            include_psu=s.pdf_include_psu,
            include_spectrum=s.pdf_include_spectrum,
            spectrum_signal=s.spectrum_signal,
            metadata=self.report_metadata(),
            markers=copy.deepcopy(self.ctrl.markers),
            benchmark=self.ctrl.benchmark,
            device=self._device_section(),
        )

        samples = samples.copy()

        def done(stats):
            if scope == "all":
                self._mark_saved_if_unchanged(len(samples))
            QtWidgets.QMessageBox.information(
                self.parent_widget, tr("Report generated"),
                tr("{n} samples, {duration}\nAverage power: {power}\nEnergy: {energy}\n\nSaved to:\n{path}",
                   n=f"{stats.count:,}", duration=format_duration(stats.duration_seconds),
                   power=format_si(stats.power_avg, "W"), energy=format_si(stats.energy_wh, "Wh"),
                   path=path))

        self._run(tr("Generating PDF report..."),
                  lambda progress: export_pdf(Path(path), samples, opts, progress), done)

    def _device_section(self) -> dict:
        """Sensor and calibration settings, for traceability in the report."""
        c = self.ctrl
        s = c.settings
        out = {}
        if c.device_config is not None:
            cfg = c.device_config
            out[tr("Averaging")] = str(cfg.avg)
            out[tr("Voltage conversion time")] = f"{cfg.vct_us} µs"
            out[tr("Current conversion time")] = f"{cfg.ict_us} µs"
            out[tr("Nominal sample rate")] = f"{cfg.sample_rate_hz:.1f} Hz"
            out[tr("Time reference")] = tr("RTC + SQW pulse (µs accurate)") if cfg.sqw \
                else tr("RTC polling (ms accurate)")
        out[tr("Shunt resistance")] = format_si(s.shunt_ohm, "Ohm")
        out[tr("Current offset")] = format_si(s.current_offset_a, "A")
        out[tr("Current gain")] = f"{s.current_gain:.5f}"
        out[tr("Voltage offset")] = format_si(s.voltage_offset_v, "V")
        out[tr("Voltage gain")] = f"{s.voltage_gain:.5f}"
        if s.host_averaging > 1:
            out[tr("Host averaging")] = str(s.host_averaging)
        if c.last_clock_sync:
            out[tr("Last synchronization")] = c.last_clock_sync
        return out

    def report_metadata(self) -> dict:
        c = self.ctrl
        meta = {}
        if c.source_name:
            meta[tr("Source")] = c.source_name
        if c.device_info is not None:
            meta[tr("Firmware")] = c.device_info.firmware
        if c.device_config is not None:
            cfg = c.device_config
            meta[tr("Sensor settings")] = tr(
                "averaging {avg}, conversion {vct}/{ict} µs ({rate})",
                avg=cfg.avg, vct=cfg.vct_us, ict=cfg.ict_us, rate=f"{cfg.sample_rate_hz:.1f} Hz")
        if c.is_v2:
            meta[tr("Lost samples")] = f"{c.lost_samples:,}"
        return meta

    def _notify_saved(self, path: str, n: int) -> None:
        QtWidgets.QMessageBox.information(
            self.parent_widget, tr("Export completed"),
            tr("{n} samples exported to:\n{path}", n=f"{n:,}", path=path))

    def _confirm_discard(self) -> bool:
        c = self.ctrl
        if not c.has_unsaved_data or not c.settings.confirm_discard:
            return True
        from .widgets.common import confirm
        return confirm(self.parent_widget, tr("Unsaved data"),
                       tr("Discard {n} unsaved samples?", n=f"{len(c.data()):,}"),
                       tr("Discard"), tr("Cancel"))

    def confirm_discard(self) -> bool:
        return self._confirm_discard()
