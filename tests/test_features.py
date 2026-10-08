"""Markers, continuous recording, benchmark figures."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from app.core import Samples, Statistics
from app.core.benchmark import BenchmarkInput, compute
from app.core.markers import MarkerList
from app.export import ReportOptions, export_csv, export_pdf
from app.export.csv_io import CsvFormat, import_recording
from app.export.recorder import RecordingWriter


def _samples(n=2000, rate=100.0, t0=0.0, p=1.0):
    t = t0 + np.arange(n) / rate
    v = np.full(n, 5.0)
    i = np.full(n, p / 5.0)
    return Samples.from_columns(t, 1.79e9 + t, v, i, v * i)


# ------------------------------------------------------------------ markers

def test_marker_list_ops_and_segments():
    ml = MarkerList()
    a = ml.add(5.0)
    b = ml.add(2.0, "boot\tdone\n")
    assert a.label == "M1" and b.label == "boot done"
    assert [m.label for m in ml.sorted()] == ["boot done", "M1"]
    ml.rename(a.id, "infer")
    ml.move(b.id, 3.0)
    segs = ml.segments(_samples(), "Start", "End")         # data spans 0..19.99 s
    assert [s.name for s in segs] == ["Start → boot done", "boot done → infer", "infer → End"]
    assert segs[1].stats.duration_seconds == pytest.approx(2.0, abs=0.02)
    assert ml.nearest(5.05, 0.1).id == a.id
    assert ml.remove(a.id) and len(ml) == 1


def test_marker_lines_replay_append_only():
    ml = MarkerList()
    for line in ("#MARKER\t1\t1.5\tA", "#MARKER\t2\t2.5\tB", "#MARKER\t1\t1.7\tA2",
                 "#MARKER\t2\tDELETE", "not a marker"):
        ml.apply_line(line)
    assert [(m.id, m.t, m.label) for m in ml.sorted()] == [(1, 1.7, "A2")]
    assert ml.add(9).id == 3            # ids never collide with loaded ones


def test_csv_export_import_keeps_markers_on_same_axis(tmp_path: Path):
    s = _samples(t0=10.0)                # selection that does not start at 0
    ml = MarkerList()
    ml.add(12.0, "in range")
    ml.add(50.0, "outside")
    p = tmp_path / "x.csv"
    export_csv(p, s, markers=ml)
    data, markers = import_recording(p)
    assert data.t[0] == 0.0
    assert [(round(m.t, 6), m.label) for m in markers.sorted()] == [(2.0, "in range")]


# ---------------------------------------------------------------- recording

def test_recording_writer_is_valid_csv_while_open(tmp_path: Path):
    w = RecordingWriter(tmp_path, CsvFormat())
    w.append(_samples(500))
    m = MarkerList().add(1.0, "a")
    w.marker_changed(m)
    w.flush()
    # Simulate a crash: the file must already be importable.
    data, markers = import_recording(w.path)
    assert len(data) == 500 and len(markers) == 1
    w.append(_samples(500, t0=5.0))
    assert w.close()
    data, _ = import_recording(w.path)
    assert len(data) == 1000
    assert [p.name for p in tmp_path.iterdir()] == [w.path.name]   # no side files


def test_short_recording_removed_and_marker_after_close_appended(tmp_path: Path):
    w = RecordingWriter(tmp_path, CsvFormat())
    w.append(_samples(10, rate=100.0))        # 0.09 s
    assert not w.close()
    assert not w.path.exists()

    w2 = RecordingWriter(tmp_path, CsvFormat())
    w2.append(_samples(300))
    assert w2.close()
    m = MarkerList().put(7, 1.25, "late")
    w2.marker_changed(m)
    w2.flush()                                # file reopened in append mode
    _, markers = import_recording(w2.path)
    assert markers.get(7).label == "late"


def test_recordings_never_overwrite(tmp_path: Path):
    from datetime import datetime
    when = datetime(2026, 10, 8, 12, 0, 0)
    a = RecordingWriter(tmp_path, CsvFormat(), started=when)
    b = RecordingWriter(tmp_path, CsvFormat(), started=when)
    assert a.path != b.path
    a.discard()
    b.discard()


# ---------------------------------------------------------------- benchmark

def test_benchmark_from_count_and_idle():
    st = Statistics.from_samples(_samples(1001, rate=100.0, p=2.0))   # 10 s at 2 W
    r = compute(st, BenchmarkInput(units=500, idle_power_w=0.5))
    assert r.energy_j == pytest.approx(20.0)
    assert r.rate == pytest.approx(50.0)
    assert r.energy_per_unit_j == pytest.approx(0.04)
    assert r.rate_per_watt == pytest.approx(25.0)
    assert r.net_power_w == pytest.approx(1.5)
    assert r.net_energy_per_unit_j == pytest.approx(0.03)
    assert r.net_rate_per_watt == pytest.approx(50.0 / 1.5)


def test_benchmark_from_rate_and_invalid_inputs():
    st = Statistics.from_samples(_samples(1001, rate=100.0, p=2.0))
    r = compute(st, BenchmarkInput(rate=30.0))
    assert r.units == pytest.approx(300.0) and r.net_power_w is None
    assert compute(st, BenchmarkInput()) is None
    assert compute(None, BenchmarkInput(units=5)) is None


def test_pdf_with_markers_and_benchmark(tmp_path: Path):
    s = _samples(3000, p=2.0)
    ml = MarkerList()
    ml.add(5.0, "A")
    ml.add(15.0, "B")
    bench = (BenchmarkInput(units=1000, idle_power_w=0.5), (5.0, 15.0), "A → B")
    p = tmp_path / "r.pdf"
    export_pdf(p, s, ReportOptions(include_spectrum=True, markers=ml, benchmark=bench,
                                   device={"Averaging": "4"}))
    assert p.read_bytes().startswith(b"%PDF")


# ------------------------------------------------------- controller flow

@pytest.mark.usefixtures("qapp")
def test_live_session_is_saved_with_markers(qapp, monkeypatch, _isolated_recordings):
    from app.core import AppSettings
    from app.serial import SerialReader
    from app.ui import controller as C
    from tests.fakes import FakePort

    class PatchedReader(SerialReader):
        def __init__(self, port, **kw):
            super().__init__(port, **kw)
            self._port = FakePort("v2", rate_hz=500)
            self.HELLO_INTERVAL_S = 0.1

    monkeypatch.setattr(C, "SerialReader", PatchedReader)
    s = AppSettings()
    s.sync_clock_on_connect = False
    ctrl = C.AcquisitionController(s)
    ctrl.start("/dev/fake")
    end = time.monotonic() + 3
    while time.monotonic() < end and ctrl.store.last_t < 1.5:
        qapp.processEvents()
        time.sleep(0.005)
    m = ctrl.add_marker()
    assert m is not None and m.t > 1.0
    ctrl.stop()
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    path = ctrl.recording_path
    assert path is not None and path.parent == _isolated_recordings
    assert not ctrl.has_unsaved_data                 # nothing to lose on quit
    ctrl.rename_marker(m.id, "after stop")           # appended to the closed file
    data, markers = import_recording(path)
    assert len(data) == len(ctrl.store)
    assert markers.get(m.id).label == "after stop"
    ctrl.shutdown()
