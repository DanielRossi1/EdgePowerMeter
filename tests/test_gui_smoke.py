"""Headless smoke tests for MainWindow data flow (no hardware, no display).

Simulates acquisition by pushing synthetic samples through the same slot the
serial reader would call, then exercises selection / export-enabling / clear.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from app.core import MeasurementRecord
from app.export import ReportGenerator


@pytest.fixture
def window(qapp):
    from app.ui.main_window import MainWindow
    win = MainWindow()
    yield win
    win.close()


def _sample(ts, v, i, p):
    return {"timestamp": ts, "voltage": v, "current": i, "power": p}


def test_window_constructs(window):
    assert window.buffers.is_empty
    assert window.full_data == []


def test_simulated_acquisition_flow(window):
    window._acquiring = True
    window._acq_start_time = time.perf_counter()

    base = datetime(2025, 1, 1, 12, 0, 0)
    for k in range(200):
        ts = base + timedelta(seconds=k * 0.0025)  # 400 Hz
        window._on_data(_sample(ts, 5.0, 0.2 + 0.001 * k, 1.0 + 0.005 * k))

    assert len(window.full_data) == 200
    assert not window.buffers.is_empty
    # Average power was tracked and is a sane positive number.
    assert window._calculate_avg_power() > 0.0


def test_selection_and_clear(window):
    window._acquiring = True
    window._acq_start_time = time.perf_counter()
    base = datetime(2025, 1, 1)
    for k in range(100):
        ts = base + timedelta(seconds=k * 0.01)
        window._on_data(_sample(ts, 5.0, 0.5, 2.5))
    window._acquiring = False

    # With no explicit region selected, selection == full data.
    assert len(window._get_selected_records()) == 100

    window._clear_data()
    assert window.full_data == []
    assert window.buffers.is_empty
    assert window._calculate_avg_power() == 0.0


def test_dragging_region_selector_updates_stats_live(window):
    # Regression: the region selector used to have no signal wired up, so
    # dragging its handles left the sidebar's Samples/Duration/Avg Power
    # labels showing the previous (often full-range) selection until an
    # unrelated pan/zoom/resize happened to refresh them.
    window._acquiring = True
    window._acq_start_time = time.perf_counter()
    base = datetime(2025, 1, 1)
    for k in range(100):
        ts = base + timedelta(seconds=k * 0.01)
        window._on_data(_sample(ts, 5.0, 0.5, 2.5))
    window._acquiring = False

    t_min = window.full_data[0].relative_time
    t_max = window.full_data[-1].relative_time
    window.plot_widget.add_region_selector(t_min, t_max)
    window._update_selection_stats()
    assert window.sel_samples_label.text() == "Samples: 100"

    # Simulate the user dragging a handle to narrow the selection.
    window.plot_widget.region.setRegion((t_min, t_min + (t_max - t_min) / 2))

    assert window.sel_samples_label.text() != "Samples: 100"
    selected = window._get_selected_records()
    assert 0 < len(selected) < 100


def test_import_populates_power_stats(window, tmp_path: Path):
    # Build a CSV on disk, then drive the import path directly (bypassing the
    # file dialog) to confirm AVG POWER is populated after import.
    records = []
    base = datetime(2025, 1, 1)
    for k in range(50):
        ts = base + timedelta(seconds=k * 0.01)
        records.append(MeasurementRecord(ts, ts.timestamp(), k * 0.01, 5.0, 0.4, 3.0))
    csv_path = tmp_path / "in.csv"
    ReportGenerator().export_csv(csv_path, records, ',')

    from app.export import CSVImporter
    imported = CSVImporter.import_csv(csv_path)

    window._clear_data()
    window.full_data = imported
    window._power_sum = sum(r.power for r in imported)
    window._power_window.clear()
    window._power_window.extend(r.power for r in imported)

    assert abs(window._calculate_avg_power() - 3.0) < 1e-6
