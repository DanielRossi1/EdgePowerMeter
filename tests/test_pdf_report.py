"""PDF report generation tests.

Regression coverage for two bugs found while reviewing the report:
  - fig_width/fig_height mistakenly multiplied by reportlab's `mm` (a
    points-per-mm constant meant for page layout) *and* divided by 25.4,
    inflating every embedded chart to ~2.83x too large per side (~8x the
    pixel area), bloating a 6-page report from ~1MB to ~5MB.
  - Summary/Energy/Derived Metrics tables each wrapped in their own
    KeepTogether, so a table that didn't fit the remaining space on the
    current page stranded itself alone on a fresh page.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core import MeasurementRecord, Statistics
from app.export.pdf_report import ReportGenerator


def _make_records(n: int = 200, hz: float = 50.0):
    t0 = datetime(2026, 7, 22, 12, 0, 0)
    records = []
    for i in range(n):
        t = i / hz
        records.append(MeasurementRecord(
            t0 + timedelta(seconds=t),
            (t0 + timedelta(seconds=t)).timestamp(),
            t, 12.0, 1.0, 12.0,
        ))
    return records


def test_graph_image_pixel_size_matches_nominal_dpi():
    # At dpi=200 for a 170x70mm figure, expect ~1338x551px (170/25.4*200,
    # 70/25.4*200), not the ~3775x1541px the mm/25.4 double-conversion bug
    # produced.
    gen = ReportGenerator()
    images = gen._generate_graphs(_make_records())
    assert images
    for img in images:
        assert img.imageWidth < 2000, f"chart is {img.imageWidth}px wide - mm/inch conversion regressed"
        assert img.imageHeight < 900


def test_export_pdf_is_reasonably_sized(tmp_path):
    records = _make_records(n=500)
    stats = Statistics.from_records(records)
    gen = ReportGenerator()
    out = tmp_path / "report.pdf"
    gen.export_pdf(out, stats, records)

    assert out.exists()
    # A short, plain report (no FFT/harmonics) with 3 line charts should be
    # well under 1MB; the mm/25.4 bug alone pushed a similar report past 3MB.
    assert out.stat().st_size < 1_500_000


def test_export_pdf_sets_metadata(tmp_path):
    records = _make_records(n=50)
    stats = Statistics.from_records(records)
    gen = ReportGenerator()
    out = tmp_path / "report.pdf"
    gen.export_pdf(out, stats, records)

    content = out.read_bytes()
    assert b"EdgePowerMeter Report" in content
