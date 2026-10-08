"""Analysis page: whole-recording overview, selection statistics, supply
quality and frequency spectrum, plus import/export."""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from ...core import PowerSupplyAnalyzer, Samples, Statistics, analyze_spectrum, rating_label
from ...core.benchmark import BenchmarkInput, compute as compute_benchmark
from ...export.units import format_duration, format_si
from ...i18n import tr
from ..controller import SOURCE_IMPORT, SOURCE_LIVE, AcquisitionController
from ..file_actions import FileActions
from ..widgets.common import Card, KeyValueList, button, combo, label, spin
from ..widgets.plot_widget import PlotWidget


class AnalysisPage(QtWidgets.QWidget):
    def __init__(self, ctrl: AcquisitionController, files: FileActions, theme, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.files = files
        self.theme = theme
        self._selection: Samples = Samples.empty()
        self._dirty_tabs = {0, 1, 2, 3, 4}
        self._segments = []
        self._next_live_refresh = 0.0
        # While True the selection tracks the whole (growing) recording.
        self._select_all_mode = True
        self._setting_region = False

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # ------------------------------------------------------------ header
        head = QtWidgets.QHBoxLayout()
        self.source_label = label("", "Muted")
        head.addWidget(self.source_label, 1)
        self.import_btn = button(tr("Import CSV"), icon_name="import", icon_color=theme.text_secondary)
        self.import_btn.clicked.connect(self.files.import_csv)
        head.addWidget(self.import_btn)
        self.scope_combo = combo([(tr("Selection"), "selection"), (tr("Whole recording"), "all")])
        self.scope_combo.setToolTip(tr("What to export"))
        head.addWidget(self.scope_combo)
        self.csv_btn = button(tr("Export CSV"), icon_name="export", icon_color=theme.text_secondary)
        self.csv_btn.clicked.connect(lambda: self.files.export_csv(*self.export_target()))
        head.addWidget(self.csv_btn)
        self.pdf_btn = button(tr("Export PDF"), "primary", "pdf", theme.accent_text)
        self.pdf_btn.clicked.connect(lambda: self.files.export_pdf(*self.export_target()))
        head.addWidget(self.pdf_btn)
        root.addLayout(head)

        # ---------------------------------------------------------- overview
        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.setChildrenCollapsible(False)
        plot_box = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(plot_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        tools = QtWidgets.QHBoxLayout()
        tools.addWidget(label(tr("Drag the highlighted region to select the range to analyze."), "Hint"))
        tools.addStretch()
        b = button(tr("Select all"), "flat")
        b.clicked.connect(self.select_all)
        tools.addWidget(b)
        b = button(tr("Zoom to selection"), "flat")
        b.clicked.connect(self._zoom_selection)
        tools.addWidget(b)
        b = button(tr("Show all"), "flat")
        b.clicked.connect(lambda: self.plot.show_all())
        tools.addWidget(b)
        pl.addLayout(tools)
        self.plot = PlotWidget(theme, ctrl.settings, overview=True)
        self.plot.region_changed.connect(self._on_region_changed)
        pl.addWidget(self.plot, 1)
        split.addWidget(plot_box)

        # -------------------------------------------------------------- tabs
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_stats_tab(), tr("Statistics"))
        self.tabs.addTab(self._build_psu_tab(), tr("Power supply quality"))
        self.tabs.addTab(self._build_spectrum_tab(), tr("Spectrum"))
        self.tabs.addTab(self._build_markers_tab(), tr("Markers"))
        self.tabs.addTab(self._build_benchmark_tab(), tr("Benchmark"))
        self.tabs.currentChanged.connect(lambda _: self._update_current_tab())
        split.addWidget(self.tabs)
        split.setSizes([420, 360])
        root.addWidget(split, 1)

        self._sel_timer = QtCore.QTimer(self)
        self._sel_timer.setSingleShot(True)
        self._sel_timer.setInterval(120)
        self._sel_timer.timeout.connect(self._update_selection)

        self.empty_hint = label(tr("No data yet. Start an acquisition or import a CSV file."), "Muted")
        self.empty_hint.setAlignment(QtCore.Qt.AlignCenter)
        root.addWidget(self.empty_hint)
        self.on_data_reset()

    # -------------------------------------------------------------- builders

    def _build_stats_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(12)
        card = Card(tr("Selection"))
        self.sel_info = KeyValueList([
            ("range", tr("Range")),
            ("duration", tr("Duration")),
            ("samples", tr("Samples")),
            ("rate", tr("Average sample rate")),
            ("energy", tr("Energy")),
            ("charge", tr("Charge")),
            ("pavg", tr("Average power")),
            ("irms", tr("Current RMS")),
        ])
        card.body.addWidget(self.sel_info)
        card.body.addStretch()
        card.setMinimumWidth(330)
        card.setMaximumWidth(420)
        lay.addWidget(card)

        card2 = Card(tr("Per quantity"))
        self.table = QtWidgets.QTableWidget(3, 5)
        self.table.setHorizontalHeaderLabels([tr("Min"), tr("Max"), tr("Average"), tr("Std dev"),
                                              tr("Peak-to-peak")])
        self.table.setVerticalHeaderLabels([tr("Voltage"), tr("Current"), tr("Power")])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ContiguousSelection)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.table.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.table.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.AdjustToContents)
        self.table.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Maximum)
        self.table.setShowGrid(False)
        card2.body.addWidget(self.table)
        card2.body.addStretch()
        lay.addWidget(card2, 1)
        return w

    def _build_psu_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(12)
        card = Card(tr("Supply rail"),
                    tr("How stable the voltage feeding the device under test is."))
        self.psu_info = KeyValueList([
            ("nominal", tr("Nominal voltage")),
            ("range", tr("Voltage range")),
            ("ripple", tr("Ripple (peak-to-peak)")),
            ("noise", tr("RMS noise")),
            ("rating", tr("Stability rating")),
            ("loadreg", tr("Load regulation")),
            ("settling", tr("Settling time")),
        ])
        card.body.addWidget(self.psu_info)
        card.setMaximumWidth(460)
        lay.addWidget(card)
        card2 = Card(tr("Assessment"))
        self.psu_text = label("", wrap=True)
        self.psu_text.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        card2.body.addWidget(self.psu_text)
        card2.body.addStretch()
        lay.addWidget(card2, 1)
        return w

    def _build_spectrum_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(12)
        left = Card(tr("Frequency spectrum"),
                    tr("Where the load variation energy is concentrated (periodic bursts, "
                       "throttling cycles, regulator ripple)."))
        top = QtWidgets.QHBoxLayout()
        top.addWidget(label(tr("Signal")))
        s = self.ctrl.settings
        self.spec_signal = combo([(tr("Current"), "current"), (tr("Power"), "power"),
                                  (tr("Voltage"), "voltage")], s.spectrum_signal)
        self.spec_signal.currentIndexChanged.connect(self._on_spec_signal)
        top.addWidget(self.spec_signal)
        self.spec_log = QtWidgets.QCheckBox(tr("Logarithmic scale"))
        self.spec_log.toggled.connect(self._on_spec_log)
        top.addWidget(self.spec_log)
        top.addStretch()
        left.body.addLayout(top)
        self.spec_plot = pg.PlotWidget()
        self.spec_plot.setMinimumHeight(160)
        self.spec_plot.setMenuEnabled(False)
        self.spec_plot.hideButtons()
        self.spec_plot.setLabel("bottom", tr("Frequency"), units="Hz")
        self.spec_curve = self.spec_plot.plot()
        self.spec_curve.setDownsampling(auto=True, method="peak")
        self.spec_curve.setClipToView(True)
        left.body.addWidget(self.spec_plot, 1)
        lay.addWidget(left, 1)

        right = Card(tr("Dominant frequencies"))
        self.peaks_label = label("", wrap=True, selectable=True)
        self.peaks_label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        right.body.addWidget(self.peaks_label)
        right.body.addStretch()
        right.setMaximumWidth(300)
        lay.addWidget(right)
        self._style_spectrum()
        return w

    def _style_spectrum(self) -> None:
        t = self.theme
        self.spec_plot.setBackground(t.plot_bg)
        for ax in ("left", "bottom"):
            a = self.spec_plot.getAxis(ax)
            a.setPen(pg.mkPen(t.border))
            a.setTextPen(pg.mkPen(t.text_secondary))
        self.spec_plot.showGrid(x=True, y=True, alpha=0.2)
        self.spec_curve.setPen(pg.mkPen(t.accent, width=1.4))

    # ------------------------------------------------------------- updates

    def on_data_reset(self) -> None:
        data = self.ctrl.data()
        self.plot.clear_data()
        self.plot.set_data(data)
        has = len(data) >= 2
        self.empty_hint.setVisible(not has)
        if has:
            self.plot.show_all()
            self.select_all()
        else:
            self._selection = Samples.empty()
            self._update_selection()
        self._update_source()

    def refresh_live(self, force: bool = False) -> None:
        """Periodic refresh while recording (only when the page is visible).

        Adaptive: the next refresh waits several times the cost of the last
        one, so very long recordings never keep the GUI busy."""
        now = time.monotonic()
        if not force and self.ctrl.is_running and now < self._next_live_refresh:
            return
        started = time.perf_counter()
        self._refresh_live()
        self._next_live_refresh = now + max(1.0, 8.0 * (time.perf_counter() - started))

    def _refresh_live(self) -> None:
        data = self.ctrl.data()
        self._update_source()
        if len(data) < 2:
            return
        self.empty_hint.setVisible(False)
        rng = self.plot.get_selected_range()
        self.plot.set_data(data)
        if rng is None or self._select_all_mode:
            self.plot.show_all()
            self.select_all()
        else:
            self.plot.refresh()

    def apply_settings(self, s) -> None:
        self.plot.apply_settings(s)

    def select_all(self) -> None:
        data = self.ctrl.data()
        if len(data) >= 2:
            self._select_all_mode = True
            self._setting_region = True
            try:
                self.plot.set_region(float(data.t[0]), float(data.t[-1]))
            finally:
                self._setting_region = False

    def _on_region_changed(self) -> None:
        if not self._setting_region:
            self._select_all_mode = False    # the user picked a range
        self._schedule_selection_update()

    def sync_spectrum_signal(self) -> None:
        idx = self.spec_signal.findData(self.ctrl.settings.spectrum_signal)
        if idx >= 0 and idx != self.spec_signal.currentIndex():
            self.spec_signal.setCurrentIndex(idx)

    def _zoom_selection(self) -> None:
        rng = self.plot.get_selected_range()
        if rng:
            pad = (rng[1] - rng[0]) * 0.05
            self.plot.zoom_to(rng[0] - pad, rng[1] + pad)

    def export_target(self):
        scope = self.scope_combo.currentData()
        if scope == "all" or self.plot.get_selected_range() is None:
            return self.ctrl.data(), "all"
        return self._current_selection(), "selection"

    def _current_selection(self) -> Samples:
        data = self.ctrl.data()
        rng = self.plot.get_selected_range()
        if rng is None:
            return data
        return data.between(*rng)

    def _update_source(self) -> None:
        c = self.ctrl
        n = len(c.data())
        if c.source == SOURCE_IMPORT:
            text = tr("Imported file: {name}", name=c.source_name)
        elif c.source == SOURCE_LIVE:
            text = tr("Live recording from {port}", port=c.source_name)
            if c.is_running:
                text += "  •  " + tr("recording")
        else:
            text = ""
        if n:
            text += ("  •  " if text else "") + tr("{n} samples", n=f"{n:,}")
        self.source_label.setText(text)
        enabled = n >= 2
        for b in (self.csv_btn, self.pdf_btn):
            b.setEnabled(enabled)

    def _schedule_selection_update(self) -> None:
        self._sel_timer.start()

    def _update_selection(self) -> None:
        self._selection = self._current_selection() if len(self.ctrl.data()) >= 2 else Samples.empty()
        self._dirty_tabs = {0, 1, 2, 3, 4}
        self._update_current_tab()

    def _update_current_tab(self) -> None:
        idx = self.tabs.currentIndex()
        if idx not in self._dirty_tabs:
            return
        self._dirty_tabs.discard(idx)
        [self._update_stats, self._update_psu, self._update_spectrum,
         self._update_markers, self._update_benchmark][idx]()

    def _update_stats(self) -> None:
        s = self._selection
        st = Statistics.from_samples(s)
        if st is None:
            for k in ("range", "duration", "samples", "rate", "energy", "charge", "pavg", "irms"):
                self.sel_info.set(k, "-")
            self.table.clearContents()
            return
        self.sel_info.set("range", f"{s.t[0]:.3f} s … {s.t[-1]:.3f} s")
        self.sel_info.set("duration", format_duration(st.duration_seconds))
        self.sel_info.set("samples", f"{st.count:,}")
        self.sel_info.set("rate", f"{st.sample_rate_hz:.1f} Hz")
        self.sel_info.set("energy", f"{format_si(st.energy_wh, 'Wh')}  ({st.energy_wh * 3600:.4g} J)")
        self.sel_info.set("charge", format_si(st.charge_ah, "Ah"))
        self.sel_info.set("pavg", format_si(st.power_avg, "W"))
        self.sel_info.set("irms", format_si(st.current_rms, "A"))
        rows = (
            ("V", st.voltage_min, st.voltage_max, st.voltage_avg, st.voltage_std),
            ("A", st.current_min, st.current_max, st.current_avg, st.current_std),
            ("W", st.power_min, st.power_max, st.power_avg, st.power_std),
        )
        for r, (unit, mn, mx, avg, sd) in enumerate(rows):
            for c, val in enumerate((mn, mx, avg, sd, mx - mn)):
                item = QtWidgets.QTableWidgetItem(format_si(val, unit))
                item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                self.table.setItem(r, c, item)

    def _update_psu(self) -> None:
        q = PowerSupplyAnalyzer().analyze_voltage_quality(self._selection)
        if q is None:
            for k in ("nominal", "range", "ripple", "noise", "rating", "loadreg", "settling"):
                self.psu_info.set(k, "-")
            self.psu_text.setText(tr("At least 10 samples are required."))
            return
        t = self.theme
        color = {"excellent": t.success, "good": t.success, "fair": t.warning,
                 "poor": t.danger}.get(q.stability_rating, "")
        self.psu_info.set("nominal", format_si(q.nominal_voltage, "V"))
        self.psu_info.set("range", f"{format_si(q.min_voltage, 'V')} … {format_si(q.max_voltage, 'V')}")
        self.psu_info.set("ripple", f"{format_si(q.voltage_ripple_mv / 1000, 'V')} "
                                    f"({q.voltage_ripple_percent:.3f} %)")
        self.psu_info.set("noise", format_si(q.rms_noise, "V"))
        self.psu_info.set("rating", rating_label(q.stability_rating), color)
        self.psu_info.set("loadreg", "-" if q.load_regulation_percent is None
                          else f"{q.load_regulation_percent:.3f} %")
        self.psu_info.set("settling", "-" if q.settling_time_ms is None
                          else f"{q.settling_time_ms:.1f} ms")
        recs = PowerSupplyAnalyzer.get_quality_recommendations(q)
        note = tr("Ripple is measured at the INA226 sample rate: faster switching ripple is "
                  "averaged out by the sensor and does not appear here.")
        self.psu_text.setText("\n\n".join(["• " + r for r in recs] + [note]))

    def _update_spectrum(self) -> None:
        res = analyze_spectrum(self._selection, self.spec_signal.currentData())
        if res is None:
            self.spec_curve.setData([], [])
            self.peaks_label.setText(tr(
                "Spectrum not available: at least 64 samples with some variation are required."))
            return
        unit = {"voltage": "V", "current": "A", "power": "W"}[res.signal]
        f, a = res.frequencies[1:], res.amplitudes[1:]
        if self.spec_log.isChecked():
            a = np.maximum(a, 1e-12)
        self.spec_curve.setData(f, a)
        self.spec_plot.setLabel("left", tr("Amplitude"), units=unit)
        lines = [f"<b>{pk.frequency:.3f} Hz</b> — {format_si(pk.amplitude, unit)}" for pk in res.peaks]
        lines.append("")
        lines.append(tr("Modulation depth: {value}", value=f"{res.modulation_percent:.2f} %"))
        lines.append(tr("Resolution: {value}", value=f"{res.resolution_hz:.3g} Hz"))
        lines.append(tr("Nyquist limit: {value}", value=f"{res.sample_rate / 2:.1f} Hz"))
        self.peaks_label.setText("<br>".join(lines))

    def _on_spec_log(self, on: bool) -> None:
        self.spec_plot.setLogMode(y=on)
        self._dirty_tabs.add(2)
        self._update_current_tab()

    def _on_spec_signal(self) -> None:
        self.ctrl.settings.spectrum_signal = self.spec_signal.currentData()
        self.ctrl.settings.save()
        self._dirty_tabs.add(2)
        self._update_current_tab()

    # -------------------------------------------------------------- markers

    def _build_markers_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(0, 10, 0, 0)
        card = Card(tr("Segments between markers"),
                    tr("Add markers with the M key or the Marker button while recording, or "
                       "right-click on a plot at the exact point. Drag a marker on this plot to "
                       "adjust it, double-click it to rename. Click a row to select that segment."))
        self.seg_table = QtWidgets.QTableWidget(0, 7)
        self.seg_table.setHorizontalHeaderLabels([
            tr("Segment"), tr("Starts at"), tr("Duration"), tr("Average power"), tr("Peak power"),
            tr("Energy"), tr("Charge")])
        self.seg_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.seg_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.seg_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.seg_table.verticalHeader().setVisible(False)
        self.seg_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.seg_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.seg_table.setShowGrid(False)
        self.seg_table.cellClicked.connect(self._select_segment)
        card.body.addWidget(self.seg_table)
        row = QtWidgets.QHBoxLayout()
        self.seg_hint = label("", "Hint")
        row.addWidget(self.seg_hint, 1)
        self.del_markers_btn = button(tr("Delete all markers"), "flat")
        row.addWidget(self.del_markers_btn)
        card.body.addLayout(row)
        lay.addWidget(card)
        return w

    def on_markers_changed(self) -> None:
        self.plot.set_markers(self.ctrl.markers.sorted())
        self._dirty_tabs.update({3, 4})
        self._update_current_tab()

    def _update_markers(self) -> None:
        data = self.ctrl.data()
        self._segments = self.ctrl.markers.segments(data, tr("Beginning"), tr("Finish"))
        self.seg_table.setRowCount(len(self._segments))
        for r, seg in enumerate(self._segments):
            st = seg.stats
            cells = [seg.name, f"{seg.start:.3f} s", format_duration(seg.end - seg.start)]
            if st is not None:
                cells += [format_si(st.energy_wh * 3600 / st.duration_seconds, "W")
                          if st.duration_seconds > 0 else "-",
                          format_si(st.power_max, "W"), format_si(st.energy_wh, "Wh"),
                          format_si(st.charge_ah, "Ah")]
            else:
                cells += ["-"] * 4
            for c, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                self.seg_table.setItem(r, c, item)
        n = len(self.ctrl.markers)
        self.seg_hint.setText(tr("{n} markers", n=n) if n else tr("No markers yet."))
        self.del_markers_btn.setEnabled(n > 0)
        self._refresh_bench_scopes()

    def _select_segment(self, row: int, _col: int = 0) -> None:
        if 0 <= row < len(self._segments):
            seg = self._segments[row]
            self._select_all_mode = False
            self.plot.set_region(seg.start, seg.end)

    # ------------------------------------------------------------ benchmark

    def _build_benchmark_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(12)

        inp = Card(tr("Workload"), tr("Enter what the device did in the analyzed range: the "
                                      "number of inferences (or frames) or the throughput."))
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.addWidget(label(tr("Range")), 0, 0)
        self.bench_scope = QtWidgets.QComboBox()
        self.bench_scope.currentIndexChanged.connect(self._on_bench_scope)
        grid.addWidget(self.bench_scope, 0, 1)
        self.bench_by_count = QtWidgets.QRadioButton(tr("Inferences completed"))
        self.bench_by_rate = QtWidgets.QRadioButton(tr("Throughput (per second)"))
        self.bench_by_count.setChecked(True)
        self.bench_count = spin(0, 2_000_000_000, 0, 0, 1)   # QSpinBox is 32-bit
        self.bench_count.setMinimumWidth(160)
        self.bench_rate = spin(0, 1e9, 0, 3, 1, " /s")
        grid.addWidget(self.bench_by_count, 1, 0)
        grid.addWidget(self.bench_count, 1, 1)
        grid.addWidget(self.bench_by_rate, 2, 0)
        grid.addWidget(self.bench_rate, 2, 1)
        grid.addWidget(label(tr("Idle power")), 3, 0)
        self.bench_idle = QtWidgets.QComboBox()
        grid.addWidget(self.bench_idle, 3, 1)
        self.bench_idle_w = spin(0, 1e4, 0, 4, 0.01, " W")
        grid.addWidget(self.bench_idle_w, 4, 1)
        inp.body.addLayout(grid)
        inp.body.addWidget(label(tr(
            "Idle power: the consumption with the device on but not working. Measure it in "
            "a segment between two markers or type it in. Net figures subtract it, so they "
            "show only the energy spent by the workload."), "Hint", wrap=True))
        self.bench_in_report = QtWidgets.QCheckBox(tr("Include in the PDF report"))
        self.bench_in_report.setChecked(True)
        inp.body.addWidget(self.bench_in_report)
        inp.body.addStretch()
        inp.setMaximumWidth(460)
        lay.addWidget(inp)

        out = Card(tr("Efficiency"))
        self.bench_out = KeyValueList([
            ("duration", tr("Duration")),
            ("power", tr("Average power")),
            ("energy", tr("Energy")),
            ("units", tr("Inferences")),
            ("rate", tr("Throughput")),
            ("epu", tr("Energy per inference")),
            ("upj", tr("Inferences per joule")),
            ("eff", tr("Throughput per watt (FPS/W)")),
            ("idle", tr("Idle power")),
            ("net_power", tr("Workload power (net)")),
            ("net_epu", tr("Energy per inference (net)")),
            ("net_eff", tr("Throughput per watt (net)")),
        ])
        out.body.addWidget(self.bench_out)
        out.body.addStretch()
        lay.addWidget(out, 1)

        for wdg in (self.bench_count, self.bench_rate, self.bench_idle_w):
            wdg.valueChanged.connect(self._on_bench_input)
        for wdg in (self.bench_by_count, self.bench_by_rate):
            wdg.toggled.connect(self._on_bench_input)
        self.bench_idle.currentIndexChanged.connect(self._on_bench_input)
        self.bench_in_report.toggled.connect(self._on_bench_input)
        self._refresh_bench_scopes()
        return w

    def _refresh_bench_scopes(self) -> None:
        segs = self.ctrl.markers.segments(self.ctrl.data(), tr("Beginning"), tr("Finish")) \
            if len(self.ctrl.markers) else []
        cur_scope = self.bench_scope.currentData()
        cur_idle = self.bench_idle.currentData()
        self.bench_scope.blockSignals(True)
        self.bench_scope.clear()
        self.bench_scope.addItem(tr("Current selection"), ("sel", None))
        for seg in segs:
            self.bench_scope.addItem(seg.name, ("seg", (seg.start, seg.end)))
        idx = self.bench_scope.findData(cur_scope) if cur_scope else 0
        self.bench_scope.setCurrentIndex(max(0, idx))
        self.bench_scope.blockSignals(False)

        self.bench_idle.blockSignals(True)
        self.bench_idle.clear()
        self.bench_idle.addItem(tr("Not considered"), ("none", None))
        self.bench_idle.addItem(tr("Typed value"), ("manual", None))
        for seg in segs:
            if seg.stats is not None and seg.stats.duration_seconds > 0:
                p = seg.stats.energy_wh * 3600 / seg.stats.duration_seconds
                self.bench_idle.addItem(f"{seg.name}  ({format_si(p, 'W')})", ("seg", p))
        idx = self.bench_idle.findData(cur_idle) if cur_idle else 0
        self.bench_idle.setCurrentIndex(max(0, idx))
        self.bench_idle.blockSignals(False)

    def _on_bench_scope(self) -> None:
        kind, rng = self.bench_scope.currentData() or ("sel", None)
        if kind == "seg" and rng:
            self._select_all_mode = False
            self.plot.set_region(*rng)       # also triggers the recomputation
        else:
            self._update_benchmark()

    def _on_bench_input(self, *_):
        self._update_benchmark()

    def _bench_samples(self) -> Samples:
        kind, rng = self.bench_scope.currentData() or ("sel", None)
        if kind == "seg" and rng:
            return self.ctrl.data().between(*rng)
        return self._selection

    def _update_benchmark(self) -> None:
        self.bench_count.setEnabled(self.bench_by_count.isChecked())
        self.bench_rate.setEnabled(self.bench_by_rate.isChecked())
        idle_kind, idle_val = self.bench_idle.currentData() or ("none", None)
        self.bench_idle_w.setVisible(idle_kind == "manual")
        idle = None
        if idle_kind == "manual":
            idle = self.bench_idle_w.value()
        elif idle_kind == "seg":
            idle = idle_val
        inp = BenchmarkInput(
            units=self.bench_count.value() if self.bench_by_count.isChecked() else None,
            rate=self.bench_rate.value() if self.bench_by_rate.isChecked() else None,
            idle_power_w=idle)
        s = self._bench_samples()
        res = compute_benchmark(Statistics.from_samples(s), inp)
        keys = ("duration", "power", "energy", "units", "rate", "epu", "upj", "eff",
                "idle", "net_power", "net_epu", "net_eff")
        if res is None:
            for k in keys:
                self.bench_out.set(k, "-")
            self.ctrl.benchmark = None
            return
        o = self.bench_out
        o.set("duration", format_duration(res.duration_s))
        o.set("power", format_si(res.avg_power_w, "W"))
        o.set("energy", f"{format_si(res.energy_j, 'J')}  ({format_si(res.energy_j / 3600, 'Wh')})")
        o.set("units", f"{res.units:,.0f}")
        o.set("rate", f"{res.rate:.3f} /s")
        o.set("epu", format_si(res.energy_per_unit_j, "J"), self.theme.accent)
        o.set("upj", f"{res.units_per_joule:.4g}")
        o.set("eff", f"{res.rate_per_watt:.4g} /s/W", self.theme.accent)
        o.set("idle", "-" if res.idle_power_w is None else format_si(res.idle_power_w, "W"))
        o.set("net_power", "-" if res.net_power_w is None else format_si(res.net_power_w, "W"))
        o.set("net_epu", "-" if res.net_energy_per_unit_j is None
              else format_si(res.net_energy_per_unit_j, "J"))
        o.set("net_eff", "-" if res.net_rate_per_watt is None else f"{res.net_rate_per_watt:.4g} /s/W")
        # Kept on the controller for the PDF report (with the range it refers to).
        if self.bench_in_report.isChecked() and len(s) >= 2:
            self.ctrl.benchmark = (inp, (float(s.t[0]), float(s.t[-1])),
                                   self.bench_scope.currentText())
        else:
            self.ctrl.benchmark = None

    def set_theme(self, theme) -> None:
        self.theme = theme
        self.plot.set_theme(theme)
        self._style_spectrum()
