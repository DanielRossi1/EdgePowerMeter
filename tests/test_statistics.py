"""Statistics: energy / charge integration correctness."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core import MeasurementRecord
from app.core.statistics import Statistics


def _const_power(n, power, current, rate_hz=100.0):
    dt = 1.0 / rate_hz
    base = datetime(2025, 1, 1)
    out = []
    for k in range(n):
        rel = k * dt
        ts = base + timedelta(seconds=rel)
        out.append(MeasurementRecord(ts, ts.timestamp(), rel, 5.0, current, power))
    return out


def test_energy_constant_power():
    # 10 W held for ~1 s (100 samples at 100 Hz -> 0.99 s span).
    recs = _const_power(100, power=10.0, current=2.0)
    stats = Statistics.from_records(recs)
    assert stats is not None
    duration = recs[-1].relative_time - recs[0].relative_time
    expected_wh = 10.0 * duration / 3600.0
    assert abs(stats.energy_wh - expected_wh) < 1e-6
    expected_ah = 2.0 * duration / 3600.0
    assert abs(stats.charge_ah - expected_ah) < 1e-6


def test_negative_power_ignored_in_energy():
    # Sensor offset can produce small negatives; they must not subtract energy.
    recs = _const_power(50, power=-1.0, current=-0.5)
    stats = Statistics.from_records(recs)
    assert stats is not None
    assert stats.energy_wh == 0.0
    assert stats.charge_ah == 0.0


def test_single_record_returns_none():
    assert Statistics.from_records(_const_power(1, 10.0, 2.0)) is None
