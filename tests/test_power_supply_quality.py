"""Power supply quality analysis edge cases."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.core import MeasurementRecord
from app.core.power_supply_quality import PowerSupplyAnalyzer


def _records(voltages, currents=None):
    currents = currents or [0.0] * len(voltages)
    base = datetime(2025, 1, 1)
    out = []
    for k, (v, c) in enumerate(zip(voltages, currents)):
        rel = k * 0.01
        ts = base + timedelta(seconds=rel)
        out.append(MeasurementRecord(ts, ts.timestamp(), rel, v, c, v * c))
    return out


def test_zero_voltage_rail_no_nan():
    # No source connected: voltages hover around 0 with tiny noise.
    volts = [1e-6 * ((-1) ** k) for k in range(50)]
    q = PowerSupplyAnalyzer().analyze_voltage_quality(_records(volts))
    assert q is not None
    assert math.isfinite(q.voltage_ripple_percent)
    assert q.voltage_ripple_percent == 0.0


def test_explicit_zero_nominal_no_crash():
    volts = [0.001 * k for k in range(50)]
    q = PowerSupplyAnalyzer().analyze_voltage_quality(_records(volts), nominal_voltage=0.0)
    assert q is not None
    assert math.isfinite(q.voltage_ripple_percent)


def test_stable_supply_rated_excellent():
    volts = [5.0 + 0.0001 * ((-1) ** k) for k in range(100)]
    q = PowerSupplyAnalyzer().analyze_voltage_quality(_records(volts))
    assert q is not None
    assert math.isfinite(q.voltage_ripple_percent)
    assert q.voltage_ripple_percent < 0.05
    assert q.stability_rating == "Excellent"


def test_insufficient_data_returns_none():
    assert PowerSupplyAnalyzer().analyze_voltage_quality(_records([5.0, 5.0])) is None
