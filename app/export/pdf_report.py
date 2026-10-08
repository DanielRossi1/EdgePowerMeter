"""PDF report generation (vector charts, no matplotlib)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.markers import MarkerList
from ..core.power_supply_quality import PowerSupplyAnalyzer, rating_label
from ..core.samples import Samples
from ..core.spectrum import analyze_spectrum
from ..core.statistics import Statistics
from ..i18n import tr
from ..version import APP_NAME, __version__
from .units import format_duration, format_si

# Report palette (print friendly, independent of the UI theme)
INK = "#1f2430"
MUTED = "#6b7280"
GRID = "#e5e7eb"
HEADER_BG = "#eef2ff"
ACCENT = "#3b5bdb"
COLOR_V = "#2563eb"
COLOR_I = "#d97706"
COLOR_P = "#059669"


@dataclass
class ReportOptions:
    title: str = ""
    notes: str = ""
    include_graphs: bool = True
    include_psu: bool = True
    include_spectrum: bool = False
    spectrum_signal: str = "current"
    metadata: Dict[str, str] = field(default_factory=dict)   # extra key/value rows
    markers: Optional[MarkerList] = None
    # (BenchmarkInput, (t0, t1), range label) from the Analysis page
    benchmark: Optional[tuple] = None
    device: Dict[str, str] = field(default_factory=dict)     # sensor + calibration


def export_pdf(path: Path, s: Samples, options: ReportOptions,
               progress: Optional[Callable[[float], None]] = None) -> Statistics:
    """Build the report for `s` and write it to `path`. Returns the statistics."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    stats = Statistics.from_samples(s)
    if stats is None:
        raise ValueError(tr("At least 2 samples are required."))

    def step(x: float) -> None:
        if progress:
            progress(x)

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=20, textColor=colors.HexColor(INK),
                        spaceAfter=4, alignment=TA_LEFT)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, textColor=colors.HexColor(ACCENT),
                        spaceBefore=10, spaceAfter=6)
    body = ParagraphStyle("Body", parent=styles["Normal"], fontSize=9.5, textColor=colors.HexColor(INK),
                          leading=13)
    muted = ParagraphStyle("Muted", parent=body, textColor=colors.HexColor(MUTED), fontSize=8.5)
    tile_value = ParagraphStyle("TileV", parent=body, fontSize=15, leading=18,
                                fontName="Helvetica-Bold", textColor=colors.HexColor(INK))
    tile_name = ParagraphStyle("TileN", parent=muted, fontSize=7.5, leading=9)

    def table(rows: List[List[str]], widths: Sequence[float], header: bool = True,
              align_right_from: int = 1) -> Table:
        t = Table(rows, colWidths=widths, hAlign="LEFT", repeatRows=1 if header else 0)
        style = [
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.8),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor(INK)),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor(GRID)),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT"),
        ]
        if header:
            style += [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(HEADER_BG)),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ]
        t.setStyle(TableStyle(style))
        return t

    def tiles(items: List[Tuple[str, str]], per_row: int = 3) -> Table:
        cells = [[Paragraph(_esc(v), tile_value), Paragraph(_esc(n.upper()), tile_name)]
                 for n, v in items]
        rows = []
        for k in range(0, len(cells), per_row):
            row = cells[k:k + per_row]
            row += [""] * (per_row - len(row))
            rows.append(row)
        w = 170 * mm / per_row
        t = Table(rows, colWidths=[w] * per_row, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(GRID)),
            ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor(GRID)),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    v = s.v.astype(np.float64)
    i = s.i.astype(np.float64)
    p = s.p.astype(np.float64)
    t0, t1 = float(s.t[0]), float(s.t[-1])
    duration = stats.duration_seconds
    energy_j = stats.energy_wh * 3600.0
    avg_power = energy_j / duration if duration > 0 else stats.power_avg
    markers = [m for m in (options.markers.sorted() if options.markers else []) if t0 <= m.t <= t1]

    story: list = []
    title = options.title.strip() or tr("Power measurement report")
    story.append(Paragraph(_esc(title), h1))
    story.append(Paragraph(_esc(tr("Generated by {app} {version} on {date}", app=APP_NAME,
                                   version=__version__,
                                   date=datetime.now().strftime("%Y-%m-%d %H:%M"))), muted))
    if options.notes.strip():
        story.append(Spacer(1, 6))
        story.append(Paragraph(_esc(options.notes).replace("\n", "<br/>"), body))
    story.append(Spacer(1, 10))

    # --- key figures
    key = [
        (tr("Average power"), format_si(avg_power, "W")),
        (tr("Energy"), f"{format_si(stats.energy_wh, 'Wh')}"),
        (tr("Duration"), format_duration(duration)),
        (tr("Peak power"), format_si(stats.power_max, "W")),
        (tr("Average current"), format_si(stats.current_avg, "A")),
        (tr("Average voltage"), format_si(stats.voltage_avg, "V")),
    ]
    bench = _benchmark_result(s, options.benchmark)
    if bench is not None:
        key.append((tr("Throughput per watt (FPS/W)"), f"{bench.rate_per_watt:.4g} /s/W"))
        key.append((tr("Energy per inference"), format_si(bench.energy_per_unit_j, "J")))
    story.append(tiles(key))
    story.append(Spacer(1, 10))

    # --- recording
    start, end = s.wall_datetime(0), s.wall_datetime(len(s) - 1)
    meta = [
        [tr("Start time"), start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if start else "-"],
        [tr("End time"), end.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if end else "-"],
        [tr("Duration"), format_duration(duration)],
        [tr("Samples"), f"{stats.count:,}"],
        [tr("Average sample rate"), f"{stats.sample_rate_hz:.1f} Hz"],
    ]
    meta += [[k, val] for k, val in options.metadata.items()]
    story.append(Paragraph(tr("Recording"), h2))
    story.append(table(meta, [55 * mm, 115 * mm], header=False, align_right_from=2))

    # --- summary per quantity
    def row(name: str, unit: str, x: np.ndarray, mn: float, mx: float, avg: float, sd: float):
        rms = float(np.sqrt(np.mean(x * x)))
        return [name] + [format_si(val, unit) for val in (mn, mx, avg, sd, rms, mx - mn)]

    summary = [
        [tr("Quantity"), tr("Min"), tr("Max"), tr("Average"), tr("Std dev"), tr("RMS"),
         tr("Peak-to-peak")],
        row(tr("Voltage"), "V", v, stats.voltage_min, stats.voltage_max, stats.voltage_avg,
            stats.voltage_std),
        row(tr("Current"), "A", i, stats.current_min, stats.current_max, stats.current_avg,
            stats.current_std),
        row(tr("Power"), "W", p, stats.power_min, stats.power_max, stats.power_avg, stats.power_std),
    ]
    crest = stats.current_max / stats.current_rms if stats.current_rms > 0 else float("nan")
    energy = [
        [tr("Quantity"), tr("Value")],
        [tr("Energy"), f"{format_si(stats.energy_wh, 'Wh')}   ({format_si(energy_j, 'J')})"],
        [tr("Charge"), f"{format_si(stats.charge_ah, 'Ah')}   ({format_si(stats.charge_ah * 3600, 'C')})"],
        [tr("Average power (energy / time)"), format_si(avg_power, "W")],
        [tr("Peak power"), format_si(stats.power_max, "W")],
        [tr("Minimum power"), format_si(stats.power_min, "W")],
        [tr("Power variability (std / mean)"),
         f"{stats.power_std / abs(stats.power_avg) * 100:.2f} %" if abs(stats.power_avg) > 1e-12 else "-"],
        [tr("Current RMS"), format_si(stats.current_rms, "A")],
        [tr("Current crest factor (peak / RMS)"), f"{crest:.3f}" if math.isfinite(crest) else "-"],
        [tr("Voltage ripple (peak-to-peak)"),
         f"{format_si(stats.voltage_max - stats.voltage_min, 'V')}   "
         f"({(stats.voltage_max - stats.voltage_min) / abs(stats.voltage_avg) * 100:.3f} %)"
         if abs(stats.voltage_avg) > 1e-9 else "-"],
    ]
    if stats.current_avg > 1e-9:
        energy.append([tr("Average load resistance"), format_si(stats.voltage_avg / stats.current_avg, "Ohm")])
    story.append(KeepTogether([
        Paragraph(tr("Summary"), h2),
        table(summary, [26 * mm] + [24 * mm] * 6),
    ]))
    story.append(KeepTogether([
        Paragraph(tr("Energy and derived quantities"), h2),
        table(energy, [80 * mm, 70 * mm]),
    ]))
    step(0.15)

    # --- markers and segments
    if markers:
        from ..core.markers import MarkerList as _ML
        ml = _ML()
        for m in markers:
            ml.put(m.id, m.t, m.label)
        rows = [[tr("Segment"), tr("Starts at"), tr("Duration"), tr("Average power"), tr("Peak power"),
                 tr("Energy")]]
        for seg in ml.segments(s, tr("Beginning"), tr("Finish")):
            st = seg.stats
            if st is None:
                continue
            avg = st.energy_wh * 3600 / st.duration_seconds if st.duration_seconds > 0 else st.power_avg
            rows.append([seg.name, f"{seg.start:.3f} s", format_duration(seg.end - seg.start),
                         format_si(avg, "W"), format_si(st.power_max, "W"), format_si(st.energy_wh, "Wh")])
        mrows = [[tr("Marker"), tr("Time"), tr("Wall-clock time")]]
        for m in markers:
            k = int(np.clip(np.searchsorted(s.t, m.t), 0, len(s) - 1))
            wall = datetime.fromtimestamp(float(s.wall[k]) + (m.t - float(s.t[k])))
            mrows.append([m.label, f"{m.t:.3f} s", wall.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]])
        story.append(Paragraph(tr("Markers and segments"), h2))
        story.append(table(mrows, [60 * mm, 30 * mm, 60 * mm]))
        story.append(Spacer(1, 6))
        story.append(table(rows, [52 * mm, 20 * mm, 24 * mm, 25 * mm, 25 * mm, 24 * mm]))

    # --- benchmark
    if bench is not None:
        inp, rng, rng_label = options.benchmark
        rows = [
            [tr("Metric"), tr("Value")],
            [tr("Range"), f"{rng_label}  ({rng[0]:.3f} s - {rng[1]:.3f} s)"],
            [tr("Duration"), format_duration(bench.duration_s)],
            [tr("Inferences"), f"{bench.units:,.0f}"],
            [tr("Throughput"), f"{bench.rate:.4g} /s"],
            [tr("Average power"), format_si(bench.avg_power_w, "W")],
            [tr("Energy"), format_si(bench.energy_j, "J")],
            [tr("Energy per inference"), format_si(bench.energy_per_unit_j, "J")],
            [tr("Inferences per joule"), f"{bench.units_per_joule:.4g}"],
            [tr("Throughput per watt (FPS/W)"), f"{bench.rate_per_watt:.4g} /s/W"],
        ]
        if bench.idle_power_w is not None:
            rows += [
                [tr("Idle power"), format_si(bench.idle_power_w, "W")],
                [tr("Workload power (net)"), format_si(bench.net_power_w, "W")],
                [tr("Energy per inference (net)"), "-" if bench.net_energy_per_unit_j is None
                 else format_si(bench.net_energy_per_unit_j, "J")],
                [tr("Throughput per watt (net)"), "-" if bench.net_rate_per_watt is None
                 else f"{bench.net_rate_per_watt:.4g} /s/W"],
            ]
        story.append(KeepTogether([Paragraph(tr("Benchmark"), h2), table(rows, [80 * mm, 80 * mm]),
                                   Spacer(1, 4),
                                   Paragraph(_esc(tr(
                                       "Net figures subtract the idle power, so they show only the "
                                       "energy spent by the workload.")), muted)]))
    step(0.25)

    # --- graphs
    if options.include_graphs:
        story.append(PageBreak())
        story.append(Paragraph(tr("Measurements over time"), h2))
        width, height = 170 * mm, 60 * mm
        mk = [(m.t - t0, m.label) for m in markers]
        for name, unit, col, color in ((tr("Voltage"), "V", v, COLOR_V),
                                       (tr("Current"), "A", i, COLOR_I),
                                       (tr("Power"), "W", p, COLOR_P)):
            subtitle = tr("min {min}   average {avg}   max {max}", min=format_si(float(col.min()), unit),
                          avg=format_si(float(col.mean()), unit), max=format_si(float(col.max()), unit))
            story.append(_time_chart(s.t, col, name, unit, color, width, height, mk, subtitle))
            story.append(Spacer(1, 4))
        story.append(Paragraph(_esc(tr(
            "Shaded band: minimum and maximum within each plotted point (spikes are preserved). "
            "Dashed line: average.")), muted))
        story.append(KeepTogether([
            Paragraph(tr("Power distribution"), h2),
            _histogram_chart(p, 170 * mm, 55 * mm),
            Spacer(1, 4),
            table([[tr("Percentile"), "1 %", "5 %", "50 %", "95 %", "99 %"],
                   [tr("Power")] + [format_si(float(np.percentile(p, q)), "W")
                                    for q in (1, 5, 50, 95, 99)]],
                  [30 * mm] + [28 * mm] * 5),
            Spacer(1, 3),
            Paragraph(_esc(tr("How long the device spent at each power level; separate peaks "
                              "reveal distinct operating states (idle, active, bursts).")), muted),
        ]))
        step(0.5)

    # --- power supply quality
    if options.include_psu:
        q = PowerSupplyAnalyzer().analyze_voltage_quality(s)
        if q is not None:
            rows = [
                [tr("Metric"), tr("Value")],
                [tr("Nominal voltage"), format_si(q.nominal_voltage, "V")],
                [tr("Voltage range"), f"{format_si(q.min_voltage, 'V')} - {format_si(q.max_voltage, 'V')}"],
                [tr("Ripple (peak-to-peak)"), f"{format_si(q.voltage_ripple_mv / 1000, 'V')}  ({q.voltage_ripple_percent:.3f} %)"],
                [tr("RMS noise"), format_si(q.rms_noise, "V")],
                [tr("Stability rating"), rating_label(q.stability_rating)],
            ]
            if q.load_regulation_percent is not None:
                rows.append([tr("Load regulation"), f"{q.load_regulation_percent:.3f} %"])
            if q.settling_time_ms is not None:
                rows.append([tr("Settling time"), f"{q.settling_time_ms:.1f} ms"])
            ok, ko = tr("Pass"), tr("Fail")
            comp = [
                [tr("Class"), tr("Requirement"), tr("Result")],
                [tr("Precision supply"), "< 0.05 %", ok if q.meets_005percent_spec else ko],
                [tr("Linear supply"), "< 0.1 %", ok if q.meets_01percent_spec else ko],
                [tr("Switching supply"), "< 1 %", ok if q.meets_1percent_spec else ko],
            ]
            block = [Paragraph(tr("Power supply quality"), h2), table(rows, [70 * mm, 80 * mm]),
                     Spacer(1, 6), table(comp, [60 * mm, 45 * mm, 45 * mm]), Spacer(1, 6)]
            for rec in PowerSupplyAnalyzer.get_quality_recommendations(q):
                block.append(Paragraph("• " + _esc(rec), body))
            block.append(Spacer(1, 3))
            block.append(Paragraph(_esc(tr(
                "Ripple is measured at the INA226 sample rate: faster switching ripple is "
                "averaged out by the sensor and does not appear here.")), muted))
            story.append(KeepTogether(block))
        step(0.7)

    # --- spectrum
    if options.include_spectrum:
        sig = options.spectrum_signal
        unit = {"voltage": "V", "current": "A", "power": "W"}.get(sig, "")
        label = {"voltage": tr("Voltage"), "current": tr("Current"), "power": tr("Power")}.get(sig, sig)
        res = analyze_spectrum(s, sig)
        story.append(PageBreak())
        story.append(Paragraph(tr("Frequency spectrum ({signal})", signal=label.lower()), h2))
        if res is None:
            story.append(Paragraph(_esc(tr(
                "Spectrum not available: at least 64 samples with some variation are required.")), body))
        else:
            story.append(Paragraph(_esc(tr(
                "Where the load variation energy is concentrated. Resolution {res}, Nyquist limit {nyq}.",
                res=f"{res.resolution_hz:.3g} Hz", nyq=f"{res.sample_rate / 2:.1f} Hz")), muted))
            story.append(_spectrum_chart(res.frequencies, res.amplitudes, unit, 170 * mm, 62 * mm))
            story.append(Spacer(1, 4))
            story.append(_spectrum_chart(res.frequencies, res.amplitudes, unit, 170 * mm, 62 * mm,
                                         db=True))
            prow = [[tr("Frequency"), tr("Amplitude"), tr("Period")]]
            prow += [[f"{pk.frequency:.3f} Hz", format_si(pk.amplitude, unit),
                      format_si(1.0 / pk.frequency, "s") if pk.frequency > 0 else "-"]
                     for pk in res.peaks]
            prow.append([tr("Modulation depth"), f"{res.modulation_percent:.2f} %", ""])
            story.append(Spacer(1, 6))
            story.append(table(prow, [50 * mm, 50 * mm, 40 * mm]))
        step(0.85)

    # --- device and calibration
    if options.device:
        story.append(KeepTogether([
            Paragraph(tr("Instrument settings"), h2),
            table([[k, val] for k, val in options.device.items()], [70 * mm, 80 * mm],
                  header=False, align_right_from=2),
        ]))

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(20 * mm, 10 * mm, f"{APP_NAME} {__version__}  -  {title}"[:110])
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, tr("Page {n}", n=doc.page))
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm, title=title, author=APP_NAME)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    step(1.0)
    return stats


def _benchmark_result(s: Samples, bench: Optional[tuple]):
    """Benchmark figures if the benchmark range lies within the exported data."""
    if not bench:
        return None
    from ..core.benchmark import compute
    inp, (b0, b1), _label = bench
    if len(s) < 2 or b0 < float(s.t[0]) - 1e-6 or b1 > float(s.t[-1]) + 1e-6:
        return None
    return compute(Statistics.from_samples(s.between(b0, b1)), inp)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ charts

def _nice_ticks(lo: float, hi: float, target: int = 5) -> List[float]:
    if not math.isfinite(lo) or not math.isfinite(hi):
        return []
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            step = m * mag
            break
    first = math.ceil(lo / step) * step
    ticks = []
    x = first
    while x <= hi + step * 1e-9:
        ticks.append(round(x, 12))
        x += step
    return ticks


def envelope(t: np.ndarray, y: np.ndarray, buckets: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Min/max per time bucket so spikes survive downsampling."""
    n = len(t)
    if n <= buckets * 2:
        return t, y, y
    edges = np.linspace(0, n, buckets + 1).astype(int)
    idx = edges[:-1]
    mins = np.minimum.reduceat(y, idx)
    maxs = np.maximum.reduceat(y, idx)
    centers = t[np.minimum(idx + np.diff(edges) // 2, n - 1)]
    return centers, mins, maxs


def _axis_scale(values: np.ndarray, unit: str) -> Tuple[float, str]:
    """Pick an SI prefix for an axis from the largest magnitude shown."""
    peak = float(np.nanmax(np.abs(values))) if len(values) else 0.0
    for factor, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ")):
        if peak >= factor or factor == 1e-6:
            return factor, prefix + unit
    return 1.0, unit


def _frame(width: float, height: float):
    from reportlab.graphics.shapes import Drawing
    d = Drawing(width, height)
    left, bottom, right, top = 46, 26, width - 8, height - 16
    return d, (left, bottom, right, top)


def _draw_axes(d, box, xticks, yticks, x2px, y2px, xfmt, yfmt, xlabel, ylabel, title, color):
    from reportlab.graphics.shapes import Line, String
    from reportlab.lib import colors
    left, bottom, right, top = box
    grid = colors.HexColor(GRID)
    ink = colors.HexColor(MUTED)
    for yt in yticks:
        y = y2px(yt)
        if bottom - 0.5 <= y <= top + 0.5:
            d.add(Line(left, y, right, y, strokeColor=grid, strokeWidth=0.5))
            d.add(String(left - 4, y - 3, yfmt(yt), fontName="Helvetica", fontSize=7,
                         fillColor=ink, textAnchor="end"))
    for xt in xticks:
        x = x2px(xt)
        if left - 0.5 <= x <= right + 0.5:
            d.add(Line(x, bottom, x, top, strokeColor=grid, strokeWidth=0.5))
            d.add(String(x, bottom - 10, xfmt(xt), fontName="Helvetica", fontSize=7,
                         fillColor=ink, textAnchor="middle"))
    d.add(Line(left, bottom, right, bottom, strokeColor=ink, strokeWidth=0.6))
    d.add(String(left, top + 5, title, fontName="Helvetica-Bold", fontSize=9,
                 fillColor=colors.HexColor(color)))
    d.add(String(right, bottom - 21, xlabel, fontName="Helvetica", fontSize=7,
                 fillColor=ink, textAnchor="end"))
    d.add(String(right, top + 5, ylabel, fontName="Helvetica", fontSize=7,
                 fillColor=ink, textAnchor="end"))


MARKER_COLOR = "#c2410c"


def _time_chart(t: np.ndarray, y: np.ndarray, name: str, unit: str, color: str,
                width: float, height: float, markers: Sequence[Tuple[float, str]] = (),
                subtitle: str = ""):
    from reportlab.graphics.shapes import Line, PolyLine, Polygon, String
    from reportlab.lib import colors

    d, box = _frame(width, height)
    left, bottom, right, top = box
    if len(t) < 2:
        return d
    factor, yunit = _axis_scale(y, unit)
    ys = y / factor
    duration = float(t[-1] - t[0])
    if duration >= 7200:
        tdiv, tunit = 3600.0, "h"
    elif duration >= 180:
        tdiv, tunit = 60.0, "min"
    else:
        tdiv, tunit = 1.0, "s"
    ts = (t - t[0]) / tdiv

    cx, lo, hi = envelope(ts, ys, int(right - left))
    ymin, ymax = float(np.min(lo)), float(np.max(hi))
    pad = (ymax - ymin) * 0.08 or max(abs(ymax) * 0.01, 1e-9)
    ymin, ymax = ymin - pad, ymax + pad
    xmax = float(ts[-1]) or 1.0

    def x2px(x):
        return left + (x / xmax) * (right - left)

    def y2px(v):
        return bottom + (v - ymin) / (ymax - ymin) * (top - bottom)

    yt = _nice_ticks(ymin, ymax, 4)
    _draw_axes(d, box, _nice_ticks(0, xmax, 8), yt, x2px, y2px,
               lambda v: f"{v:g}", lambda v: f"{v:.4g}",
               tr("Time [{unit}]", unit=tunit), f"[{yunit}]", name, color)

    stroke = colors.HexColor(color)
    if len(cx) < len(ts):
        # Shaded min/max band plus the mid line.
        pts = [c for x, v in zip(cx, hi) for c in (x2px(x), y2px(v))]
        pts += [c for x, v in zip(cx[::-1], lo[::-1]) for c in (x2px(x), y2px(v))]
        fill = colors.Color(stroke.red, stroke.green, stroke.blue, alpha=0.35)
        d.add(Polygon(pts, fillColor=fill, strokeColor=None, strokeWidth=0))
        mid = (lo + hi) / 2
        d.add(PolyLine([c for x, v in zip(cx, mid) for c in (x2px(x), y2px(v))],
                       strokeColor=stroke, strokeWidth=0.6))
    else:
        d.add(PolyLine([c for x, v in zip(cx, lo) for c in (x2px(x), y2px(v))],
                       strokeColor=stroke, strokeWidth=0.9))
    avg = float(np.mean(ys))
    d.add(Line(left, y2px(avg), right, y2px(avg), strokeColor=stroke, strokeWidth=0.5,
               strokeDashArray=[3, 2]))
    mcolor = colors.HexColor(MARKER_COLOR)
    for mt, mlabel in markers:
        x = x2px(mt / tdiv)
        if left <= x <= right:
            d.add(Line(x, bottom, x, top, strokeColor=mcolor, strokeWidth=0.7,
                       strokeDashArray=[2, 2]))
            d.add(String(x + 2, top - 8, mlabel[:24], fontName="Helvetica", fontSize=6.5,
                         fillColor=mcolor))
    if subtitle:
        d.add(String(left + 70, top + 5, subtitle, fontName="Helvetica", fontSize=7,
                     fillColor=colors.HexColor(MUTED)))
    return d


def _spectrum_chart(freqs: np.ndarray, amps: np.ndarray, unit: str, width: float, height: float,
                    db: bool = False):
    from reportlab.graphics.shapes import PolyLine
    from reportlab.lib import colors

    d, box = _frame(width, height)
    left, bottom, right, top = box
    if len(freqs) < 2:
        return d
    f = freqs[1:]
    a = amps[1:]
    if db:
        # dB relative to the strongest component: shows small periodic
        # components that vanish on a linear scale.
        ref = float(np.max(a)) or 1.0
        a = 20.0 * np.log10(np.maximum(a / ref, 1e-6))
        fx, _, hi = envelope(f, a, int(right - left))
        ymin, ymax = max(-100.0, float(np.min(hi))), 5.0
        aunit = "dB"
    else:
        factor, aunit = _axis_scale(a, unit)
        a = a / factor
        fx, _, hi = envelope(f, a, int(right - left))
        ymin, ymax = 0.0, float(np.max(hi)) * 1.1 or 1.0
    xmax = float(f[-1]) or 1.0

    def x2px(x):
        return left + (x / xmax) * (right - left)

    def y2px(v):
        return bottom + (min(max(v, ymin), ymax) - ymin) / (ymax - ymin) * (top - bottom)

    title = tr("Amplitude (dB, relative to the strongest component)") if db else tr("Amplitude")
    _draw_axes(d, box, _nice_ticks(0, xmax, 8), _nice_ticks(ymin, ymax, 4), x2px, y2px,
               lambda v: f"{v:g}", lambda v: f"{v:.3g}",
               tr("Frequency [Hz]"), f"[{aunit}]", title, ACCENT)
    d.add(PolyLine([c for x, v in zip(fx, hi) for c in (x2px(x), y2px(v))],
                   strokeColor=colors.HexColor(ACCENT), strokeWidth=0.7))
    return d


def _histogram_chart(values: np.ndarray, width: float, height: float, bins: int = 60):
    """Share of time spent at each power level."""
    from reportlab.graphics.shapes import Rect
    from reportlab.lib import colors

    d, box = _frame(width, height)
    left, bottom, right, top = box
    if len(values) < 2:
        return d
    factor, unit = _axis_scale(values, "W")
    x = values / factor
    lo, hi = float(np.min(x)), float(np.max(x))
    if hi <= lo:
        hi = lo + (abs(lo) * 0.01 or 1e-6)
    counts, edges = np.histogram(x, bins=bins, range=(lo, hi))
    share = counts / counts.sum() * 100.0
    ymax = float(share.max()) * 1.1 or 1.0

    def x2px(v):
        return left + (v - lo) / (hi - lo) * (right - left)

    def y2px(v):
        return bottom + v / ymax * (top - bottom)

    _draw_axes(d, box, _nice_ticks(lo, hi, 8), _nice_ticks(0, ymax, 4), x2px, y2px,
               lambda v: f"{v:.4g}", lambda v: f"{v:g}",
               f"{tr('Power')} [{unit}]", "[%]", tr("Share of time"), COLOR_P)
    fill = colors.HexColor(COLOR_P)
    for k, pct in enumerate(share):
        if pct <= 0:
            continue
        x0, x1 = x2px(edges[k]), x2px(edges[k + 1])
        d.add(Rect(x0, bottom, max(0.5, x1 - x0 - 0.6), y2px(pct) - bottom,
                   fillColor=fill, strokeColor=None, strokeWidth=0))
    return d
