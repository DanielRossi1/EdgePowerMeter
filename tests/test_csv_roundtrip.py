"""CSV export -> import round-trip tests.

Focus: high sample-rate data whose millisecond-truncated timestamps collide.
The RelativeTime column must be used to preserve every sample and its timing.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app.core import MeasurementRecord
from app.export import ReportGenerator, CSVImporter


def _make_records(n: int, rate_hz: float) -> list[MeasurementRecord]:
    """Build n records at the given rate. Timestamps only carry ms precision."""
    dt = 1.0 / rate_hz
    base = datetime(2025, 1, 1, 12, 0, 0)
    records = []
    for k in range(n):
        rel = k * dt
        ts = base + timedelta(seconds=rel)
        records.append(MeasurementRecord(
            timestamp=ts,
            unix_time=ts.timestamp(),
            relative_time=rel,
            voltage=5.0 + 0.001 * k,
            current=0.1 + 0.0001 * k,
            power=(5.0 + 0.001 * k) * (0.1 + 0.0001 * k),
        ))
    return records


def test_roundtrip_high_rate_preserves_all_samples(tmp_path: Path):
    # 400 Hz => 2.5 ms spacing; several samples share the same millisecond.
    records = _make_records(1000, rate_hz=400.0)
    csv_path = tmp_path / "hi.csv"

    ReportGenerator().export_csv(csv_path, records, separator=',')
    imported = CSVImporter.import_csv(csv_path)

    # No sample lost despite colliding millisecond timestamps.
    assert len(imported) == len(records)


def test_roundtrip_relative_time_precision(tmp_path: Path):
    records = _make_records(500, rate_hz=400.0)
    csv_path = tmp_path / "hi.csv"

    ReportGenerator().export_csv(csv_path, records, separator=',')
    imported = CSVImporter.import_csv(csv_path)

    # Relative times survive with sub-millisecond fidelity (column, not timestamp).
    for orig, imp in zip(records, imported):
        assert abs(orig.relative_time - imp.relative_time) < 1e-6
    # And they are strictly monotonic (no ties that a plot buffer would drop).
    rels = [r.relative_time for r in imported]
    assert all(b > a for a, b in zip(rels, rels[1:]))


def test_roundtrip_values_preserved(tmp_path: Path):
    records = _make_records(50, rate_hz=100.0)
    csv_path = tmp_path / "v.csv"
    ReportGenerator().export_csv(csv_path, records, separator=';')
    imported = CSVImporter.import_csv(csv_path)

    assert len(imported) == len(records)
    for orig, imp in zip(records, imported):
        assert abs(orig.voltage - imp.voltage) < 1e-6
        assert abs(orig.current - imp.current) < 1e-6
        assert abs(orig.power - imp.power) < 1e-6


def test_import_legacy_4col_format(tmp_path: Path):
    """Old 4-column CSV (no RelativeTime) still imports via timestamp."""
    csv_path = tmp_path / "legacy.csv"
    csv_path.write_text(
        "Timestamp,Voltage,Current,Power\n"
        "2025-01-01 12:00:00.000,5.0,0.1,0.5\n"
        "2025-01-01 12:00:01.000,5.1,0.2,1.02\n"
        "2025-01-01 12:00:02.000,5.2,0.3,1.56\n"
        "2025-01-01 12:00:03.000,5.3,0.4,2.12\n"
    )
    imported = CSVImporter.import_csv(csv_path)
    assert len(imported) == 4
    assert imported[0].relative_time == 0.0
    assert abs(imported[-1].relative_time - 3.0) < 1e-6
