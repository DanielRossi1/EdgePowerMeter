"""Live page: real-time plots and metric tiles."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...export.units import format_duration, format_si
from ...i18n import tr
from ..controller import AcquisitionController
from ..icons import icon
from ..widgets.common import KeyValueList, label, select_data
from ..widgets.metric_card import MetricCard
from ..widgets.plot_widget import PlotWidget

WINDOW_CHOICES = (1, 2, 5, 10, 30, 60, 120, 300, 600, 1800)


def _window_label(sec: float) -> str:
    if sec < 60:
        return tr("{n} s", n=f"{sec:g}")
    minutes = sec / 60
    return tr("{n} min", n=f"{minutes:g}" if minutes == int(minutes) else f"{minutes:.1f}")


class LivePage(QtWidgets.QWidget):
    def __init__(self, ctrl: AcquisitionController, theme, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.theme = theme
        s = ctrl.settings

        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        # ---------------------------------------------------------- plot area
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(8)
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(6)

        self.series_buttons = {}
        for key, text, attr in (("v", tr("Voltage"), "show_voltage"),
                                ("i", tr("Current"), "show_current"),
                                ("p", tr("Power"), "show_power")):
            b = QtWidgets.QToolButton()
            b.setObjectName("Chip")
            b.setText(text)
            b.setCheckable(True)
            b.setChecked(getattr(s, attr))
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.toggled.connect(lambda on, k=key, a=attr: self._toggle_series(k, a, on))
            self.series_buttons[key] = b
            bar.addWidget(b)
        bar.addSpacing(10)
        self.marker_btn = QtWidgets.QToolButton()
        self.marker_btn.setObjectName("Chip")
        self.marker_btn.setText(tr("Marker"))
        self.marker_btn.setIcon(icon("flag", theme.warning, 16))
        self.marker_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.marker_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.marker_btn.setToolTip(tr("Add a marker now (key M). Right-click on the plot to add "
                                      "one at a precise point; double-click a marker to rename it."))
        bar.addWidget(self.marker_btn)
        bar.addStretch()

        bar.addWidget(label(tr("Window"), "Muted"))
        self.window_combo = QtWidgets.QComboBox()
        for sec in WINDOW_CHOICES:
            self.window_combo.addItem(_window_label(sec), float(sec))
        self.window_combo.setToolTip(tr("Visible time span. The mouse wheel on the plot changes it too."))
        self.window_combo.currentIndexChanged.connect(self._on_window_combo)
        bar.addWidget(self.window_combo)

        self.follow_btn = QtWidgets.QToolButton()
        self.follow_btn.setObjectName("Chip")
        self.follow_btn.setText(tr("Follow live"))
        self.follow_btn.setCheckable(True)
        self.follow_btn.setChecked(True)
        self.follow_btn.setIcon(icon("follow", theme.text_secondary, 16))
        self.follow_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.follow_btn.setToolTip(tr("Keep the newest data in view. Drag the plot to look back; "
                                      "double-click it to return to live."))
        self.follow_btn.toggled.connect(lambda on: self.plot.set_follow(on))
        bar.addWidget(self.follow_btn)
        left.addLayout(bar)

        self.plot = PlotWidget(theme, s)
        self.plot.follow_changed.connect(self._on_follow_changed)
        self.plot.window_changed.connect(self._on_plot_window_changed)
        left.addWidget(self.plot, 1)
        root.addLayout(left, 1)
        self._sync_window_combo(self.plot.window_seconds)

        # -------------------------------------------------------- side panel
        side_widget = QtWidgets.QWidget()
        side_widget.setFixedWidth(272)
        side = QtWidgets.QVBoxLayout(side_widget)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(8)
        self.card_v = MetricCard(tr("Voltage"), "V", theme.chart_voltage)
        self.card_i = MetricCard(tr("Current"), "A", theme.chart_current)
        self.card_p = MetricCard(tr("Power"), "W", theme.chart_power)
        self.card_avg = MetricCard(tr("Average power"), "W", theme.accent)
        for c in (self.card_v, self.card_i, self.card_p, self.card_avg):
            side.addWidget(c)

        details = QtWidgets.QFrame()
        details.setObjectName("Card")
        dl = QtWidgets.QVBoxLayout(details)
        dl.setContentsMargins(14, 12, 14, 12)
        self.info = KeyValueList([
            ("energy", tr("Energy")),
            ("charge", tr("Charge")),
            ("pmax", tr("Peak power")),
            ("duration", tr("Duration")),
            ("samples", tr("Samples")),
            ("rate", tr("Rate")),           # compact: the side panel is narrow
            ("lost", tr("Lost samples")),
        ])
        self.info.setToolTip(tr("Energy and charge integrate power and current over the device "
                                "time axis (trapezoidal rule); disconnection gaps are skipped."))
        dl.addWidget(self.info)
        side.addWidget(details)
        side.addStretch()
        root.addWidget(side_widget)

        self.apply_settings(s)

    # ------------------------------------------------------------- updates

    def refresh(self) -> None:
        """Called by the main window once per frame when data changed."""
        self.plot.set_data(self.ctrl.data())
        self.plot.refresh()

    def update_metrics(self) -> None:
        """Lower-rate update of the numeric tiles (readable, ~8 Hz)."""
        s = self.ctrl.settings
        data = self.ctrl.data()
        st = self.ctrl.stats
        mode, dec = s.unit_mode, s.value_decimals
        if not len(data):
            for c in (self.card_v, self.card_i, self.card_p, self.card_avg):
                c.set_value(None)
                c.set_secondary("")
            for k in ("energy", "charge", "pmax", "duration", "samples", "rate", "lost"):
                self.info.set(k, "-")
            return
        self.card_v.set_value(float(data.v[-1]), mode, dec)
        self.card_i.set_value(float(data.i[-1]), mode, dec)
        self.card_p.set_value(float(data.p[-1]), mode, dec)
        if s.avg_power_mode == "window":
            self.card_avg.set_value(self.ctrl.window_avg_power(s.avg_window_s), mode, dec)
            self.card_avg.set_secondary(tr("Last {window}", window=format_duration(s.avg_window_s)))
        else:
            self.card_avg.set_value(st.power_avg, mode, dec)
            self.card_avg.set_secondary(tr("Whole recording"))
        rng = lambda lo, hi, u: f"{format_si(lo, u, 3)} … {format_si(hi, u, 3)}"  # noqa: E731
        self.card_v.set_secondary(rng(st.v_min, st.v_max, "V"))
        self.card_i.set_secondary(rng(st.i_min, st.i_max, "A"))
        self.card_p.set_secondary(rng(st.p_min, st.p_max, "W"))

        self.info.set("energy", format_si(st.energy_ws / 3600.0, "Wh"))
        self.info.set("charge", format_si(st.charge_as / 3600.0, "Ah"))
        self.info.set("pmax", format_si(st.p_max, "W"))
        self.info.set("duration", format_duration(data.duration))
        self.info.set("samples", f"{len(data):,}")
        rate = self.ctrl.recent_rate() if self.ctrl.is_running else (
            (len(data) - 1) / data.duration if data.duration > 0 else 0.0)
        self.info.set("rate", f"{rate:.1f} Hz")
        lost = self.ctrl.lost_samples
        if self.ctrl.is_v2 or lost:
            total = len(data) + lost
            pct = lost / total * 100 if total else 0.0
            self.info.set("lost", f"{lost:,} ({pct:.2f} %)",
                          self.theme.warning if lost else "")
        else:
            self.info.set("lost", tr("n/a"))

    def on_data_reset(self) -> None:
        self.plot.clear_data()
        self.plot.set_data(self.ctrl.data())
        if len(self.ctrl.data()) and not self.ctrl.is_running:
            # Imported / stopped recording: show it whole. While streaming
            # (e.g. a rebuild after a language change) keep following live.
            self.plot.set_follow(False)
            self.plot.show_all()
        else:
            self.plot.refresh()
        self.update_metrics()

    def apply_settings(self, s) -> None:
        self.plot.apply_settings(s)
        for key, attr in (("v", "show_voltage"), ("i", "show_current"), ("p", "show_power")):
            b = self.series_buttons[key]
            b.blockSignals(True)
            b.setChecked(getattr(s, attr))
            b.blockSignals(False)

    # ------------------------------------------------------------- handlers

    def _toggle_series(self, key: str, attr: str, on: bool) -> None:
        s = self.ctrl.settings
        if not on and sum(getattr(s, a) for a in ("show_voltage", "show_current", "show_power")) <= 1:
            # Keep at least one plot visible.
            self.series_buttons[key].blockSignals(True)
            self.series_buttons[key].setChecked(True)
            self.series_buttons[key].blockSignals(False)
            return
        setattr(s, attr, on)
        s.save()
        self.plot.set_series_visible(key, on)

    def _on_window_combo(self) -> None:
        sec = self.window_combo.currentData()
        if sec:
            self.plot.set_window_seconds(float(sec))

    def _on_plot_window_changed(self, sec: float) -> None:
        self._sync_window_combo(sec)

    def _sync_window_combo(self, sec: float) -> None:
        idx = self.window_combo.findData(float(sec))
        if idx < 0:
            # Free zoom (mouse wheel): show the exact value as a temporary entry.
            # Signals stay blocked while the previous temporary entry (the
            # current one) is removed, or the combo would jump to another item.
            self.window_combo.blockSignals(True)
            for i in range(self.window_combo.count() - 1, -1, -1):
                if self.window_combo.itemData(i) not in WINDOW_CHOICES:
                    self.window_combo.removeItem(i)
            text = tr("{n} s", n=f"{sec:.1f}") if sec < 60 else _window_label(sec)
            self.window_combo.addItem(text, float(sec))
            self.window_combo.setCurrentIndex(self.window_combo.count() - 1)
            self.window_combo.blockSignals(False)
        else:
            select_data(self.window_combo, float(sec))

    def _on_follow_changed(self, on: bool) -> None:
        self.follow_btn.blockSignals(True)
        self.follow_btn.setChecked(on)
        self.follow_btn.blockSignals(False)
