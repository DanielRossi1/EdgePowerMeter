"""Regression tests for GUI bugs found by adversarial testing (markers, import,
calibration, recordings folder, float32 range)."""
from __future__ import annotations

import sys
import time

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6 import QtWidgets

from app.core import AppSettings, Samples
from app.export.csv_io import import_recording_ex
from app.export.units import format_si


def _pump(qapp, seconds: float, until=None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if until is not None and until():
            return
        time.sleep(0.002)


def _samples(n: int, i: float = 0.1) -> Samples:
    t = np.arange(n) / 1000.0
    return Samples.from_columns(t, 1.7e9 + t, np.full(n, 4.0), np.full(n, i), np.full(n, 4.0 * i))


@pytest.fixture
def window(qapp, monkeypatch):
    import app.ui.main_window as mw

    def load():
        s = AppSettings()
        s.use_opengl = False      # offscreen platform has no QOpenGLWidget
        s.confirm_discard = False
        s.language = "en"         # assertions compare English text
        return s

    monkeypatch.setattr(mw.AppSettings, "load", staticmethod(load))
    win = mw.MainWindow()
    win.resize(1400, 900)
    win.show()
    qapp.processEvents()
    yield win
    win.ctrl.has_unsaved_data = False
    win.close()


@pytest.fixture
def excs(monkeypatch):
    """Exceptions raised inside Qt slots reach sys.excepthook; collect them."""
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda et, ev, tb: seen.append(ev))
    return seen


def _ordered_entries(plot):
    return sorted(plot._marker_items.values(), key=lambda e: e.t)


# ------------------------------------------------ 1. braces in marker labels

@pytest.mark.parametrize("label", ["{value}", "{0}", "{", "{ch1}", "a}b"])
def test_marker_labels_with_braces_are_drawn_literally(qapp, window, excs, label):
    window.ctrl.load(_samples(3000), "braces")
    plot = window.live.plot
    window.ctrl.add_marker(0.5, "ok-before")
    window.ctrl.add_marker(1.0, label)
    window.ctrl.add_marker(1.5, "ok-after")
    _pump(qapp, 0.2, lambda: len(plot._marker_items) == 3)
    assert excs == []
    assert len(plot._marker_items) == 3
    assert len(plot._marker_lines) == 3 * len(plot._plots)
    texts = [e.lines[0][1].label.toPlainText() for e in _ordered_entries(plot)]
    assert texts == ["ok-before", label, "ok-after"]


# --------------------------------------------- 2. incremental, coalesced markers

def test_two_thousand_markers_added_one_by_one_are_fast_and_all_drawn(qapp, window, excs):
    window.ctrl.load(_samples(20000), "many")
    plot = window.live.plot
    t0 = time.monotonic()
    for k in range(2000):
        window.ctrl.add_marker(k * 0.001, f"m{k}")
    # One coalesced redraw after the burst; the lines are then drawn once.
    _pump(qapp, 4.0, lambda: len(plot._marker_items) == 2000)
    elapsed = time.monotonic() - t0
    assert excs == []
    assert elapsed < 5.0, f"2000 markers took {elapsed:.2f} s"
    assert len(plot._marker_items) == 2000
    assert len(plot._marker_lines) == 2000 * len(plot._plots)


def test_marker_burst_triggers_one_refresh_per_view(qapp, window):
    window.ctrl.load(_samples(3000), "burst")
    # Let the refreshes queued by load() finish before counting the burst's.
    _pump(qapp, 2.0, lambda: not (window._markers_timer.isActive()
                                  or window.analysis._markers_timer.isActive()))
    calls = {"live": [], "analysis": []}
    live, analysis = window.live.plot, window.analysis.plot
    orig_live, orig_analysis = live.set_markers, analysis.set_markers
    live.set_markers = lambda m: (calls["live"].append(len(m)), orig_live(m))
    analysis.set_markers = lambda m: (calls["analysis"].append(len(m)), orig_analysis(m))
    for k in range(50):
        window.ctrl.add_marker(k * 0.01, f"b{k}")
    _pump(qapp, 0.2)
    assert calls == {"live": [50], "analysis": [50]}


def test_remove_and_edit_touch_only_the_changed_marker(qapp, window):
    window.ctrl.load(_samples(5000), "few")
    ids = [window.ctrl.add_marker(k * 0.1, f"m{k}").id for k in range(20)]
    plot = window.live.plot
    _pump(qapp, 0.1, lambda: len(plot._marker_items) == 20)
    before = {mid: [line for _, line in plot._marker_items[mid].lines] for mid in ids}

    def unchanged(mid):
        now = [line for _, line in plot._marker_items[mid].lines]
        return len(now) == len(before[mid]) and all(a is b for a, b in zip(now, before[mid]))

    # Remove one: the others keep their very same line objects.
    window.ctrl.remove_marker(ids[3])
    _pump(qapp, 0.1)
    assert ids[3] not in plot._marker_items
    assert all(unchanged(m) for m in ids if m != ids[3])

    # Rename one: only it is redrawn.
    window.ctrl.rename_marker(ids[5], "renamed")
    _pump(qapp, 0.1)
    assert plot._marker_items[ids[5]].label == "renamed"
    assert not unchanged(ids[5])
    assert all(unchanged(m) for m in ids if m not in (ids[3], ids[5]))

    # Move one: repositioned in place, no new line objects anywhere.
    lines_before = [line for _, line in plot._marker_items[ids[7]].lines]
    window.ctrl.move_marker(ids[7], 2.25)
    _pump(qapp, 0.1)
    assert all(a is b for a, b in zip(
        [line for _, line in plot._marker_items[ids[7]].lines], lines_before))
    assert all(line.value() == pytest.approx(2.25) for line in lines_before)
    assert all(unchanged(m) for m in ids if m not in (ids[3], ids[5], ids[7]))


# ------------------------------------------- 3. non-finite time / plot ranges

def test_csv_import_drops_non_finite_relative_time(qapp, window, excs, tmp_path):
    p = tmp_path / "inf.csv"
    p.write_text("time,relative_s,voltage_V,current_A,power_W\n"
                 "1,0,4,0.1,0.4\n2,inf,4,0.1,0.4\n3,1,4,0.1,0.4\n")
    samples, _, skipped = import_recording_ex(p)
    assert len(samples) == 2 and skipped == 1
    assert np.isfinite(samples.t).all()
    window.ctrl.load(samples, "inf.csv")
    _pump(qapp, 0.1)
    assert excs == []


def test_csv_import_rebase_overflow_drops_only_the_overflowing_row(tmp_path):
    p = tmp_path / "span.csv"
    p.write_text("time,relative_s,voltage_V,current_A,power_W\n"
                 "1,-1e308,4,0.1,0.4\n2,1e308,4,0.1,0.4\n3,0,4,0.1,0.4\n")
    samples, _, skipped = import_recording_ex(p)
    assert np.isfinite(samples.t).all()
    assert len(samples) == 2 and skipped == 1
    assert samples.t[0] == 0.0


def test_plot_ignores_non_finite_ranges(qapp, window, excs):
    plot = window.live.plot
    plot._set_x_range(float("-inf"), float("inf"))
    plot._set_x_range(float("nan"), 1.0)
    plot.set_data(Samples(np.array([0.0, np.inf]), np.array([1.7e9, 1.7e9]),
                          np.ones(2, np.float32), np.ones(2, np.float32), np.ones(2, np.float32)))
    plot.show_all()
    _pump(qapp, 0.05)
    assert excs == []


# ------------------------------------------------------ 4. zero offset range

def test_zero_now_out_of_range_offset_is_not_applied(qapp, window, monkeypatch):
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a[2])))
    window.ctrl.load(_samples(3000, i=2.0), "load2A")     # needs -2 A: outside the spin's +-1 A
    dev, s = window.device, window.ctrl.settings
    before_spin, before_settings = dev.i_offset.value(), s.current_offset_a
    dev._zero_current()
    assert warned == ["offset out of range, not applied"]
    assert dev.i_offset.value() == before_spin
    assert s.current_offset_a == before_settings
    assert "offset out of range, not applied" in dev.log.toPlainText()


def test_zero_now_in_range_offset_is_applied_and_logged(qapp, window):
    window.ctrl.load(_samples(3000, i=0.5), "load500mA")
    dev, s = window.device, window.ctrl.settings
    dev._zero_current()
    assert s.current_offset_a == pytest.approx(-0.5, abs=1e-6)
    assert dev.i_offset.value() == pytest.approx(-500.0, abs=1e-3)
    assert "Current zero set: offset " + format_si(s.current_offset_a, "A") in dev.log.toPlainText()


# ------------------------------------------------ 5. recordings folder with NUL

def test_settings_reject_nul_in_recordings_dir():
    s = AppSettings()
    s.recordings_dir = "bad\x00dir"
    s.validate()
    assert s.recordings_dir == ""


def test_open_recording_with_nul_in_folder_reports_instead_of_raising(qapp, window):
    ctrl = window.ctrl
    ctrl.settings.autosave = True
    ctrl.settings.recordings_dir = "bad\x00dir"       # set past validation on purpose
    levels = []
    ctrl.message.connect(lambda level, text: levels.append(level))
    ctrl._open_recording()
    assert ctrl.writer is None
    assert "warning" in levels


# ------------------------------------------------------ 6. float32 overflow

def test_float32_overflow_rows_are_dropped_and_columns_stay_aligned():
    t = [0.0, 1.0, 2.0, 3.0, 4.0]
    s = Samples.from_columns(t, [0.0] * 5, [1e30, 1e308, 2.0, np.nan, 5.0],
                             [1.0, 1e308, 1.0, 1.0, -1e308], [1.0, 1.0, 1.0, 1.0, 1e39])
    # Rows 1 and 4 overflow float32 and are dropped; the NaN row is kept as given.
    assert list(s.t) == [0.0, 2.0, 3.0]
    assert [len(c) for c in (s.wall, s.v, s.i, s.p)] == [3, 3, 3, 3]
    assert s.v[0] == np.float32(1e30) and s.v[1] == np.float32(2.0)
    assert np.isnan(s.v[2])
    assert np.isfinite(s.i).all() and np.isfinite(s.p).all()


def test_non_finite_input_passes_through_unchanged():
    s = Samples.from_columns([0.0, 1.0], [0.0, 0.0], [np.nan, np.inf], [1.0, 1.0], [1.0, 1.0])
    assert len(s) == 2
    assert np.isnan(s.v[0]) and np.isinf(s.v[1])


def test_float32_range_values_within_limits_are_kept():
    s = Samples.from_columns([0.0, 1.0], [0.0, 0.0], [1e30, -1e30], [1e30, 1.0], [1.0, 1.0])
    assert len(s) == 2


def test_csv_import_drops_values_beyond_float32_and_counts_them(tmp_path):
    p = tmp_path / "huge.csv"
    p.write_text("time,relative_s,voltage_V,current_A,power_W\n"
                 "1,0,1e308,1e308,1e308\n2,1,1,1,1\n3,2,1,1,1\n")
    samples, _, skipped = import_recording_ex(p)
    assert len(samples) == 2 and skipped == 1
    assert np.isfinite(samples.v).all() and np.isfinite(samples.p).all()


def test_from_columns_stays_vectorized_on_large_batches():
    n = 1_000_000
    t = np.arange(n) / 1000.0
    start = time.monotonic()
    s = Samples.from_columns(t, t, np.full(n, 4.0), np.full(n, 0.1), np.full(n, 0.4))
    assert time.monotonic() - start < 1.0
    assert len(s) == n


# ----------------------------------------------------- 7. import busy guard

def test_import_is_refused_while_another_job_runs(qapp, window, monkeypatch):
    dialog_calls = []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (dialog_calls.append(1), ("", ""))[1]))
    messages = []
    window.ctrl.message.connect(lambda level, text: messages.append((level, text)))
    window.files._jobs.add(object())               # an import/export is running
    try:
        window.files.import_csv()
    finally:
        window.files._jobs.clear()
    assert dialog_calls == []
    assert messages and messages[0][0] == "info"


# ------------------------------------------------------ 8. shunt sanity hint

def test_unusual_shunt_value_shows_hint_without_blocking(qapp, window):
    dev = window.device
    dev.shunt.setValue(2.0)
    assert not dev.shunt_hint.isHidden()
    assert "unusual shunt value, check the resistor" in dev.shunt_hint.text()
    assert window.ctrl.settings.shunt_ohm == pytest.approx(2.0)     # accepted, not blocked
    dev.shunt.setValue(0.0004)
    assert not dev.shunt_hint.isHidden()
    dev.shunt.setValue(0.010)
    assert dev.shunt_hint.isHidden()
