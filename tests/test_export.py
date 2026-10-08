"""CSV round trip (all layouts/locales) and PDF report."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.core import Samples
from app.export import ReportOptions, export_csv, export_pdf, format_si, import_csv


def _samples(n=4000, rate=400.0):
    t = np.arange(n) / rate
    v = 5.0 + 0.01 * np.sin(t)
    i = 0.2 + 0.05 * np.cos(3 * t)
    return Samples.from_columns(t, 1_790_000_000.0 + t, v, i, v * i)


@pytest.mark.parametrize("sep,dec,fmt", [(",", ".", "datetime"), (";", ",", "datetime"),
                                         ("\t", ".", "epoch"), (",", ",", "epoch")])
def test_csv_roundtrip(tmp_path: Path, sep, dec, fmt):
    s = _samples()
    p = tmp_path / "x.csv"
    export_csv(p, s, sep, dec, fmt)
    r = import_csv(p)
    assert len(r) == len(s)
    assert np.allclose(r.t, s.t, atol=1e-6)
    assert np.allclose(r.v, s.v, atol=1e-5)
    assert np.allclose(r.p, s.p, atol=1e-5)
    assert np.allclose(r.wall, s.wall, atol=2e-3)


def test_high_rate_samples_not_merged(tmp_path: Path):
    s = _samples(5000, rate=2000.0)     # several samples per millisecond timestamp
    p = tmp_path / "fast.csv"
    export_csv(p, s)
    assert len(import_csv(p)) == 5000


def test_import_legacy_four_columns(tmp_path: Path):
    p = tmp_path / "old.csv"
    p.write_text("Timestamp,Voltage[V],Current[A],Power[W]\n"
                 "2025-11-30 12:00:00.000,5.0,0.1,0.5\n"
                 "2025-11-30 12:00:00.500,5.1,0.2,1.02\n")
    r = import_csv(p)
    assert r.t.tolist() == pytest.approx([0.0, 0.5])
    assert r.v[1] == pytest.approx(5.1)


def test_import_rejects_garbage(tmp_path: Path):
    p = tmp_path / "bad.csv"
    p.write_text("hello\nworld\n")
    with pytest.raises(ValueError):
        import_csv(p)


def test_pdf_report(tmp_path: Path):
    p = tmp_path / "r.pdf"
    stats = export_pdf(p, _samples(), ReportOptions(title="My <test> & report", notes="DUT: board",
                                                    include_spectrum=True,
                                                    metadata={"Firmware": "2.0.0"}))
    data = p.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(data) < 500_000
    assert b"My <test> & report" in data or b"My" in data
    assert stats.count == 4000


def test_pdf_requires_two_samples(tmp_path: Path):
    with pytest.raises(ValueError):
        export_pdf(tmp_path / "r.pdf", _samples(1), ReportOptions())


def test_format_si():
    assert format_si(0.01234, "A") == "12.34 mA"
    assert format_si(1234.5, "W") == "1.234 kW"   # 4 significant digits (round-half-even)
    assert format_si(0, "V") == "0 V"
    assert format_si(float("nan"), "V") == "-"


def test_import_firmware_serial_log(tmp_path: Path):
    p = tmp_path / "log.csv"
    p.write_text("=== EdgePowerMeter ===\n[INFO] Initializing...\n# EdgePowerMeter v2.0.0 ready\n"
                 "Timestamp,Voltage[V],Current[A],Power[W]\n"
                 "2026-10-08 12:00:00.000,5.0,0.1,0.5\n2026-10-08 12:00:00.010,5.0,0.1,0.5\n")
    assert len(import_csv(p)) == 2


def test_export_keeps_microamp_resolution(tmp_path: Path):
    i = np.array([0.25e-6, 0.75e-6, 1.75e-6])
    s = Samples.from_columns([0, 1, 2], [1.79e9] * 3, [3.3] * 3, i, 3.3 * i)
    p = tmp_path / "x.csv"
    export_csv(p, s)
    assert np.allclose(import_csv(p).i, i, rtol=1e-5)
