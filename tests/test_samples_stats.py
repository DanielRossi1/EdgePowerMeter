"""SampleStore, Statistics, RunningStats, power supply quality, spectrum."""
from __future__ import annotations

import numpy as np
import pytest

from app.core import (PowerSupplyAnalyzer, RunningStats, Samples, SampleStore, Statistics,
                      analyze_spectrum)


def _samples(n=1000, rate=1000.0, v=5.0, i=0.2, t0=0.0):
    t = t0 + np.arange(n) / rate
    vv = np.full(n, v)
    ii = np.full(n, i) if np.isscalar(i) else i
    return Samples.from_columns(t, 1.79e9 + t, vv, ii, vv * ii)


def test_store_grows_and_views():
    st = SampleStore()
    for k in range(5):
        st.append(_samples(40000, t0=k * 40.0))
    assert len(st) == 200000
    view = st.view()
    assert view.t[-1] == pytest.approx(4 * 40.0 + 39999 / 1000.0)
    assert st.nbytes == 200000 * (8 + 8 + 4 * 3)
    st.clear()
    assert st.is_empty


def test_between_selects_inclusive_range():
    s = _samples(11, rate=1.0)
    sub = s.between(2.0, 5.0)
    assert sub.t.tolist() == [2.0, 3.0, 4.0, 5.0]


def test_energy_constant_power():
    st = Statistics.from_samples(_samples(3601, rate=1.0, v=5.0, i=0.2))  # 1 W for 1 h
    assert st.energy_wh == pytest.approx(1.0)
    assert st.charge_ah == pytest.approx(0.2)
    assert st.sample_rate_hz == pytest.approx(1.0)


def test_negative_power_ignored_in_energy():
    s = _samples(100, v=0.0, i=-0.001)
    assert Statistics.from_samples(s).energy_wh == 0.0


def test_gap_not_integrated():
    a = _samples(1000, rate=1000.0)
    b = _samples(1000, rate=1000.0, t0=100.0)   # 99 s outage
    st = Statistics.from_samples(a.concat(b))
    assert st.energy_wh == pytest.approx(2 * 0.999 / 3600, rel=1e-3)


def test_zero_duration_reports_no_energy():
    s = Samples.from_columns([1.0, 1.0], [0, 0], [5, 5], [1, 1], [5, 5])
    st = Statistics.from_samples(s)
    assert st.duration_seconds == 0.0 and st.energy_wh == 0.0


def test_single_sample_returns_none():
    assert Statistics.from_samples(_samples(1)) is None


def test_running_stats_matches_batch_statistics():
    rng = np.random.default_rng(1)
    i = 0.2 + rng.normal(0, 0.05, 5000)
    s = _samples(5000, i=i)
    rs = RunningStats()
    for k in range(0, 5000, 333):
        rs.update(s.slice(k, k + 333))
    st = Statistics.from_samples(s)
    assert rs.energy_ws / 3600 == pytest.approx(st.energy_wh, rel=1e-9)
    assert rs.power_avg == pytest.approx(st.power_avg, rel=1e-6)
    assert rs.count == 5000


def test_psu_zero_volt_rail_no_nan():
    q = PowerSupplyAnalyzer().analyze_voltage_quality(_samples(100, v=0.0))
    assert q.voltage_ripple_percent == 0.0


def test_psu_stable_supply_excellent():
    q = PowerSupplyAnalyzer().analyze_voltage_quality(_samples(200, v=12.0))
    assert q.stability_rating == "excellent"


def test_psu_load_step_detected_time_based():
    n = 2000
    i = np.where(np.arange(n) < 1000, 0.1, 0.6)
    v = np.where(np.arange(n) < 1000, 5.0, 4.95)
    s = Samples.from_columns(np.arange(n) / 1000.0, np.zeros(n), v, i, v * i)
    q = PowerSupplyAnalyzer().analyze_voltage_quality(s)
    assert q.load_regulation_percent == pytest.approx(0.05 / 4.975 * 100, rel=0.05)
    assert q.settling_time_ms is not None and q.settling_time_ms < 5


def test_psu_insufficient_data():
    assert PowerSupplyAnalyzer().analyze_voltage_quality(_samples(5)) is None


def test_spectrum_finds_sine_frequency_and_amplitude():
    n, rate = 8000, 1000.0
    t = np.arange(n) / rate
    i = 0.5 + 0.1 * np.sin(2 * np.pi * 50 * t)
    s = Samples.from_columns(t, t, np.ones(n), i, i)
    res = analyze_spectrum(s, "current")
    assert res.dominant.frequency == pytest.approx(50, abs=0.2)
    assert res.dominant.amplitude == pytest.approx(0.1, rel=0.05)


def test_spectrum_requires_variation():
    assert analyze_spectrum(_samples(1000), "current") is None
