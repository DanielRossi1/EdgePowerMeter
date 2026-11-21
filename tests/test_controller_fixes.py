"""Regression tests for controller recording, reconnect and file-action rules."""
from __future__ import annotations

import threading
import time
import warnings
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6 import QtCore, QtWidgets

from app.core import Samples
from app.core.markers import MarkerList
from app.export.csv_io import CsvFormat
from app.export.recorder import RecordingWriter
from app.ui import controller as C
from app.ui.file_actions import FileActions
from tests.fakes import FakePort
from tests.test_reader_controller import _pump, ctrl  # noqa: F401  (fixture)


def _use_folder(ctrl, tmp_path) -> None:
    ctrl.settings.autosave = True
    ctrl.settings.recordings_dir = str(tmp_path)


def _samples(n: int) -> Samples:
    t = np.linspace(0.0, n / 1000.0, n)
    return Samples.from_columns(t, t, np.ones(n), np.ones(n), np.ones(n))


def test_start_within_stop_delay_keeps_new_recording(qapp, ctrl, tmp_path):
    """Regression: the delayed finish of the stopped recording closed the new one."""
    _use_folder(ctrl, tmp_path)
    ctrl.start("/dev/fake")
    _pump(qapp, 3.0, lambda: len(ctrl.store) > 1200)     # long enough to be kept
    first = ctrl.writer
    ctrl.stop()
    ctrl.start("/dev/fake")                      # well within RECORDING_FINISH_DELAY_MS
    _pump(qapp, 2.0, lambda: ctrl.writer is not first)   # the new file opens with the port
    second = ctrl.writer
    assert second is not first and second.is_open
    _pump(qapp, 0.8)                             # past the delay
    assert second.is_open and ctrl.writer is second
    assert ctrl._flush_timer.isActive()
    assert first.path.exists()
    ctrl.stop()


def test_stop_while_reconnecting_finishes_recording_now(qapp, ctrl, tmp_path):
    """Regression: Stop while RECONNECTING left the writer open with the flush timer running."""
    _use_folder(ctrl, tmp_path)
    ctrl._ports["list"] = []                     # the device does not come back
    ctrl._ports["fakes"] = [FakePort("v2", disconnect_after_s=0.4)]
    ctrl.start("/dev/fake")
    _pump(qapp, 3.0, lambda: ctrl.state == C.STATE_RECONNECTING)
    assert ctrl.state == C.STATE_RECONNECTING
    w = ctrl.writer
    assert w.is_open
    ctrl.stop()
    assert not w.is_open
    assert not ctrl._flush_timer.isActive()


def test_disconnect_without_auto_reconnect_finishes_recording(qapp, ctrl, tmp_path):
    """Regression: the IDLE transition on disconnect left the writer open."""
    _use_folder(ctrl, tmp_path)
    ctrl.settings.auto_reconnect = False
    ctrl._ports["fakes"] = [FakePort("v2", disconnect_after_s=0.4)]
    ctrl.start("/dev/fake")
    _pump(qapp, 2.0, lambda: ctrl.writer is not None)
    w = ctrl.writer
    assert w.is_open
    _pump(qapp, 3.0, lambda: ctrl.state == C.STATE_IDLE)
    assert ctrl.state == C.STATE_IDLE
    assert not w.is_open
    assert not ctrl._flush_timer.isActive()


def _port_info(device, serial, vid=0x303A, pid=0x1001):
    return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=serial,
                           description=None, hwid=None)


def test_replugged_under_other_name_reconnects(qapp, ctrl, monkeypatch):
    """Regression: a device back as ttyACM1 was never found again."""
    infos = [_port_info("/dev/fake", "A1")]
    monkeypatch.setattr(C.PortDiscovery, "_comports", classmethod(lambda cls: list(infos)))
    ctrl._ports["list"] = []
    ctrl._ports["fakes"] = [FakePort("v2", disconnect_after_s=0.4), FakePort("v2")]
    ctrl.start("/dev/fake")
    _pump(qapp, 3.0, lambda: ctrl.state == C.STATE_RECONNECTING)
    assert ctrl.state == C.STATE_RECONNECTING
    infos[:] = [_port_info("/dev/ttyACM1", "A1")]     # the device re-enumerates under a new name
    ctrl._ports["list"] = ["/dev/ttyACM1"]
    _pump(qapp, 4.0, lambda: ctrl.state == C.STATE_STREAMING)
    assert ctrl.state == C.STATE_STREAMING
    assert ctrl.port == "/dev/ttyACM1" and ctrl.source_name == "/dev/ttyACM1"
    ctrl.stop()


def test_usb_identity_requires_serial_number(monkeypatch):
    infos = {
        "/dev/a": SimpleNamespace(vid=1, pid=2, serial_number="X"),
        "/dev/b": SimpleNamespace(vid=1, pid=2, serial_number=None),
    }
    monkeypatch.setattr(C.PortDiscovery, "get_port_info", staticmethod(infos.get))
    assert C.AcquisitionController._usb_identity("/dev/a") == (1, 2, "X")
    assert C.AcquisitionController._usb_identity("/dev/b") is None
    assert C.AcquisitionController._usb_identity("/dev/none") is None


_ORIG_LIST_PORTS = C.PortDiscovery.__dict__["list_ports"]   # before the fixture patches it


def _reconnecting(ctrl, port, usb_id):
    ctrl.port = port
    ctrl._usb_id = usb_id
    ctrl._set_state(C.STATE_RECONNECTING)
    ctrl._resume_host_time = time.time()


def test_reconnect_poll_enumerates_once_with_identity(monkeypatch, ctrl):
    """Regression: each poll re-enumerated the ports once per port (N+1 scans on the GUI thread)."""
    calls = []
    infos = [_port_info(f"/dev/ttyACM{i}", f"S{i}") for i in range(33)]
    monkeypatch.setattr(C.PortDiscovery, "_comports",
                        classmethod(lambda cls: calls.append(1) or list(infos)))
    _reconnecting(ctrl, "/dev/fake", (0x303A, 0x1001, "GONE"))
    ctrl._poll_reconnect()
    assert len(calls) == 1
    assert ctrl.state == C.STATE_RECONNECTING


def test_reconnect_poll_enumerates_once_by_name(monkeypatch, ctrl):
    calls = []
    monkeypatch.setattr(C.PortDiscovery, "list_ports", _ORIG_LIST_PORTS)
    monkeypatch.setattr(C.PortDiscovery, "_comports",
                        classmethod(lambda cls: calls.append(1) or []))
    _reconnecting(ctrl, "/dev/fake", None)
    ctrl._poll_reconnect()
    assert len(calls) == 1


def test_wrong_device_on_old_name_is_not_reopened(monkeypatch, ctrl):
    """Regression: a different adapter that took the old name was reopened as the meter."""
    infos = [_port_info("/dev/fake", "OTHER")]
    monkeypatch.setattr(C.PortDiscovery, "_comports", classmethod(lambda cls: list(infos)))
    opened = []
    monkeypatch.setattr(ctrl, "_open_reader", lambda: opened.append(1))
    _reconnecting(ctrl, "/dev/fake", (0x303A, 0x1001, "A1"))
    ctrl._poll_reconnect()
    assert not opened
    assert ctrl.port == "/dev/fake"
    assert ctrl.state == C.STATE_RECONNECTING


def test_identity_match_wins_over_wrong_device_on_old_name(monkeypatch, ctrl):
    infos = [_port_info("/dev/fake", "OTHER"), _port_info("/dev/ttyACM1", "A1")]
    monkeypatch.setattr(C.PortDiscovery, "_comports", classmethod(lambda cls: list(infos)))
    opened = []
    monkeypatch.setattr(ctrl, "_open_reader", lambda: opened.append(1))
    _reconnecting(ctrl, "/dev/fake", (0x303A, 0x1001, "A1"))
    ctrl._poll_reconnect()
    assert opened and ctrl.port == "/dev/ttyACM1"


def test_identical_adapters_prefer_old_name(monkeypatch, ctrl):
    infos = [_port_info("/dev/ttyACM0", "A1"), _port_info("/dev/fake", "A1")]
    monkeypatch.setattr(C.PortDiscovery, "_comports", classmethod(lambda cls: list(infos)))
    opened = []
    monkeypatch.setattr(ctrl, "_open_reader", lambda: opened.append(1))
    _reconnecting(ctrl, "/dev/fake", (0x303A, 0x1001, "A1"))
    ctrl._poll_reconnect()
    assert opened and ctrl.port == "/dev/fake"


def test_reap_runs_on_gui_thread(qapp, ctrl, monkeypatch):
    """Regression: _reap ran on the reader thread and mutated _stopping off the GUI thread."""
    t = _SlowThread()
    reaped_on = []
    monkeypatch.setattr(t, "deleteLater", lambda: reaped_on.append(threading.current_thread()))
    t.start()
    ctrl._release(t)
    _pump(qapp, 2.0, lambda: reaped_on)
    assert reaped_on == [threading.main_thread()]
    assert not ctrl._stopping


def test_release_of_unconnected_thread_is_silent(qapp, ctrl):
    """Regression: releasing a reader with no samples_ready slot printed a RuntimeWarning."""
    t = _SlowThread()
    t.start()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ctrl._release(t)
    assert not [w for w in caught if issubclass(w.category, RuntimeWarning)]
    _pump(qapp, 2.0, lambda: not ctrl._stopping)


def test_clear_rebases_lost_samples(qapp, ctrl):
    """Regression: the lost count of the previous run reappeared after Clear."""
    ctrl._ports["fakes"] = [FakePort("v2", skip_seq_every=10)]
    ctrl.start("/dev/fake")
    _pump(qapp, 3.0, lambda: ctrl.lost_samples > 50)
    assert ctrl.lost_samples > 50
    ctrl.clear()
    assert ctrl.lost_samples == 0
    n = len(ctrl.store)
    _pump(qapp, 2.0, lambda: len(ctrl.store) > n)
    assert len(ctrl.store) > n
    assert ctrl.lost_samples < 50
    ctrl.stop()


class _SlowThread(QtCore.QThread):
    samples_ready = QtCore.Signal(object)

    def run(self) -> None:
        time.sleep(0.4)


def test_release_defers_deletion_of_running_thread(qapp, ctrl):
    """Regression: deleting a QThread that is still running aborts the process."""
    t = _SlowThread()
    t.start()
    ctrl._release(t)
    assert t.isRunning()
    assert any(x is t for x in ctrl._stopping)
    _pump(qapp, 2.0, lambda: not ctrl._stopping)
    assert not ctrl._stopping
    assert not t.isRunning()


def test_clear_while_recording_keeps_file(qapp, ctrl, tmp_path):
    """Regression: Clear during a live run deleted the recording file."""
    _use_folder(ctrl, tmp_path)
    ctrl.start("/dev/fake")
    _pump(qapp, 3.0, lambda: len(ctrl.store) > 1600)
    first = ctrl.writer
    path = first.path
    ctrl.clear()
    assert path.exists() and not first.is_open
    assert ctrl.writer is not None and ctrl.writer is not first and ctrl.writer.is_open
    assert ctrl.writer.path != path
    assert not ctrl.has_unsaved_data
    assert len(path.read_text(encoding="utf-8").splitlines()) > 1000
    ctrl.stop()


def test_short_recording_with_samples_is_unsaved(qapp, ctrl, tmp_path):
    """Regression: a recording under 1 s was deleted without marking the in-memory data unsaved."""
    _use_folder(ctrl, tmp_path)
    ctrl.start("/dev/fake")
    _pump(qapp, 2.0, lambda: len(ctrl.store) > 200)
    ctrl.stop()
    _pump(qapp, 0.6)
    assert ctrl.writer is None
    assert len(ctrl.store) > 0
    assert ctrl.has_unsaved_data


def test_already_saved_offer_skipped_after_write_error(qapp, ctrl, tmp_path, monkeypatch):
    _use_folder(ctrl, tmp_path)
    w = RecordingWriter(tmp_path, CsvFormat(ctrl.settings.csv_separator,
                                            ctrl.settings.csv_decimal,
                                            ctrl.settings.csv_time_format))
    w.append(_samples(2000))
    w.close()
    ctrl.writer = w
    files = FileActions(ctrl, QtWidgets.QWidget())
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self: asked.append(1) or 0)

    w.error = "No space left on device"
    assert files._already_saved() is False
    assert not asked

    w.error = None                               # finished without error: offer the file
    assert files._already_saved() is True
    assert asked


def test_import_result_not_loaded_when_acquisition_started(qapp, ctrl, tmp_path, monkeypatch):
    """Regression: the import finished after Start and stopped the acquisition."""
    from app.ui import file_actions as FA

    csv = tmp_path / "in.csv"
    csv.write_text("x", encoding="utf-8")
    ctrl.settings.save = lambda: None
    captured = {}
    shown = []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(csv), "")))
    monkeypatch.setattr(FA.FileActions, "_run",
                        lambda self, title, fn, on_success: captured.update(done=on_success))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a)))
    files = FileActions(ctrl, QtWidgets.QWidget())
    files.import_csv()
    ctrl._set_state(C.STATE_STREAMING)           # Start pressed while reading
    captured["done"]((_samples(100), MarkerList()))
    assert ctrl.source != C.SOURCE_IMPORT
    assert len(ctrl.store) == 0
    assert shown
    ctrl._set_state(C.STATE_IDLE)


def test_start_and_clear_refused_while_file_job_runs(ctrl, monkeypatch):
    from app.ui.main_window import MainWindow

    messages, started, cleared = [], [], []
    monkeypatch.setattr(ctrl, "start", lambda port: started.append(port))
    monkeypatch.setattr(ctrl, "clear", lambda: cleared.append(1))
    window = SimpleNamespace(
        ctrl=ctrl,
        files=SimpleNamespace(busy=True, confirm_discard=lambda: True),
        statusBar=lambda: SimpleNamespace(showMessage=lambda text, ms: messages.append(text)),
    )
    MainWindow.toggle_acquisition(window)
    MainWindow._clear(window)
    assert not started and not cleared
    assert len(messages) == 2


class _UnopenablePort(FakePort):
    def open(self):
        raise ConnectionError("no such port")


def test_failed_start_keeps_loaded_data_markers_and_unsaved_flag(qapp, ctrl):
    """Regression: Start cleared the data before the port was opened, so a
    port that failed to open destroyed the loaded or recorded data."""
    markers = MarkerList()
    markers.add(0.5, "keep me")
    ctrl.load(_samples(500), "old.csv", markers)
    ctrl.has_unsaved_data = True
    ctrl._ports["fakes"] = [_UnopenablePort("v2")]
    ctrl.start("/dev/fake")
    _pump(qapp, 3.0, lambda: ctrl.state == C.STATE_IDLE)
    _pump(qapp, 0.3)
    assert ctrl.state == C.STATE_IDLE
    assert len(ctrl.store) == 500
    assert len(ctrl.markers) == 1
    assert ctrl.has_unsaved_data
    assert ctrl.source == C.SOURCE_IMPORT
    assert ctrl.source_name == "old.csv"


def test_failed_start_creates_no_recording_file(qapp, ctrl, tmp_path):
    """Regression: the autosave file was created before the port opened and
    was left behind when the port failed."""
    _use_folder(ctrl, tmp_path)
    ctrl._ports["fakes"] = [_UnopenablePort("v2")]
    ctrl.start("/dev/fake")
    _pump(qapp, 3.0, lambda: ctrl.state == C.STATE_IDLE)
    _pump(qapp, 0.6)                             # past the finish delay
    assert ctrl.writer is None
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
