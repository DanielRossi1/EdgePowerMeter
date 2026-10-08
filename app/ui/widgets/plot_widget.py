"""Stacked voltage / current / power plots with a sliding time window.

Two modes:
  * live (default): follows the newest data with a configurable window;
    wheel changes the window length, dragging pans and pauses following,
    double-click resumes following.
  * overview: shows the whole recording and a draggable selection region
    (mirrored on every visible plot) used by the Analysis page.

Only the visible slice is handed to pyqtgraph, which then peak-downsamples it
to the pixel width, so redraw cost does not grow with recording length.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal

from ...core.samples import Samples
from ...i18n import N_, tr
from ..theme import ThemeColors

# Whether OpenGL rendering has been probed and found usable.
# None = not yet checked, True/False = checked and cached.
_opengl_checked: Optional[bool] = None


def _opengl_actually_works() -> bool:
    """Verify a real GL context can be created, not just that the PyOpenGL
    bindings import. Sandboxed installs (snap/flatpak) can ship PyOpenGL while
    the host GL driver fails to load; trusting the import would switch
    pyqtgraph to QOpenGLWidget and break the whole window."""
    try:
        import OpenGL  # noqa: F401
    except ImportError:
        return False
    try:
        from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
        surface = QOffscreenSurface()
        surface.create()
        if not surface.isValid():
            return False
        ctx = QOpenGLContext()
        if not ctx.create():
            return False
        return bool(ctx.makeCurrent(surface))
    except Exception:
        return False


def _opengl_available(enabled: bool = True) -> bool:
    """Cached OpenGL capability check (needs a QApplication).

    `enabled=False` (user setting) skips the probe and forces raster rendering.
    """
    global _opengl_checked
    if not enabled:
        pg.setConfigOptions(useOpenGL=False, enableExperimental=False)
        return False
    if _opengl_checked is None:
        _opengl_checked = _opengl_actually_works()
        pg.setConfigOptions(useOpenGL=_opengl_checked, enableExperimental=_opengl_checked)
    return _opengl_checked


def _decimate(x: np.ndarray, y: np.ndarray, width_px: int) -> Tuple[np.ndarray, np.ndarray]:
    """Min/max envelope with about two points per pixel.

    pyqtgraph's own peak downsampling still walks every sample on every
    redraw (≈0.7 s for one hour at 1 kHz); reducing here first keeps redraws
    cheap and independent of the recording length, while spikes survive
    because each bucket contributes both its minimum and its maximum.
    """
    n = len(x)
    bucket = n // width_px
    if bucket < 4:
        return x, y
    m = (n // bucket) * bucket
    yb = y[:m].reshape(-1, bucket)
    lo = yb.min(axis=1)
    hi = yb.max(axis=1)
    xb = x[:m:bucket]
    xs = np.repeat(xb, 2)
    ys = np.empty(2 * len(lo), dtype=y.dtype)
    ys[0::2] = lo
    ys[1::2] = hi
    if m < n:                       # keep the newest samples exactly
        xs = np.concatenate((xs, x[m:]))
        ys = np.concatenate((ys, y[m:]))
    return xs, ys


SERIES: Tuple[Tuple[str, str, str], ...] = (
    ("v", N_("Voltage"), "V"),
    ("i", N_("Current"), "A"),
    ("p", N_("Power"), "W"),
)


class PlotWidget(pg.GraphicsLayoutWidget):
    view_changed = Signal()
    cursor_values = Signal(float, float, float, float)   # t, v, i, p
    cursor_left = Signal()
    region_changed = Signal()
    follow_changed = Signal(bool)
    # Markers: (marker id, new time) after a drag; id on double-click;
    # (time under cursor, nearest marker id or 0, global QPoint) on right-click.
    marker_moved = Signal(int, float)
    marker_activated = Signal(int)
    context_requested = Signal(float, int, object)
    window_changed = Signal(float)

    MIN_WINDOW_S = 0.2
    MAX_WINDOW_S = 3600.0
    ZOOM_FACTOR = 1.25

    def __init__(self, theme: ThemeColors, settings, overview: bool = False, parent=None):
        _opengl_available(getattr(settings, "use_opengl", True))
        super().__init__(parent)
        self.theme = theme
        self.settings = settings
        self.overview = overview
        self._data = Samples.empty()
        self._window_s = float(settings.time_window_s)
        self._follow = not overview
        self._updating = False
        self._visible = {"v": settings.show_voltage, "i": settings.show_current,
                         "p": settings.show_power}
        self._plots: Dict[str, pg.PlotItem] = {}
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._vlines: List[pg.InfiniteLine] = []
        self._regions: List[pg.LinearRegionItem] = []
        self._region_values: Optional[Tuple[float, float]] = None
        self._markers: list = []                      # Marker objects
        self._dense = False
        self._marker_lines: List[pg.InfiniteLine] = []
        self._syncing_region = False

        self.setBackground(theme.plot_bg)
        self.ci.setSpacing(4)
        self.ci.setContentsMargins(4, 4, 8, 4)
        self.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self._build()

    # ------------------------------------------------------------- building

    def _build(self) -> None:
        self.ci.clear()
        self._plots.clear()
        self._curves.clear()
        self._vlines.clear()
        self._marker_lines = []
        self._regions.clear()

        keys = [k for k, _, _ in SERIES if self._visible[k]] or ["p"]
        first: Optional[pg.PlotItem] = None
        for row, key in enumerate(keys):
            name, unit = next((n, u) for k, n, u in SERIES if k == key)
            plot = self.addPlot(row=row, col=0)
            plot.setLabel("left", tr(name), units=unit)
            plot.getAxis("left").setWidth(64)
            plot.getAxis("left").enableAutoSIPrefix(self.settings.unit_mode == "auto")
            if row == len(keys) - 1:
                plot.setLabel("bottom", tr("Time"), units="s")
            else:
                plot.getAxis("bottom").setStyle(showValues=False)
            plot.setMouseEnabled(x=True, y=False)
            plot.enableAutoRange(axis="x", enable=False)
            plot.enableAutoRange(axis="y", enable=True)
            plot.hideButtons()
            plot.setMenuEnabled(False)
            if first is None:
                first = plot
                plot.getViewBox().sigXRangeChanged.connect(self._on_x_range_changed)
            else:
                plot.setXLink(first)
            curve = plot.plot(antialias=self.settings.antialias)
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            vline = pg.InfiniteLine(angle=90, movable=False)
            vline.setVisible(False)
            plot.addItem(vline, ignoreBounds=True)
            self._plots[key] = plot
            self._curves[key] = curve
            self._vlines.append(vline)
            if self.overview:
                region = pg.LinearRegionItem(movable=True)
                region.setZValue(10)
                region.setVisible(False)
                region.sigRegionChanged.connect(self._on_region_moved)
                plot.addItem(region, ignoreBounds=True)
                self._regions.append(region)

        self.apply_style()
        self._draw_markers()
        if self._region_values is not None:
            self.set_region(*self._region_values)
        self.refresh()

    def apply_style(self) -> None:
        t = self.theme
        self.setBackground(t.plot_bg)
        colors = {"v": t.chart_voltage, "i": t.chart_current, "p": t.chart_power}
        for key, plot in self._plots.items():
            for ax_name in ("left", "bottom"):
                ax = plot.getAxis(ax_name)
                ax.setPen(pg.mkPen(t.border))
                ax.setTextPen(pg.mkPen(t.text_secondary))
                ax.label.setDefaultTextColor(pg.mkColor(colors[key] if ax_name == "left" else t.text_secondary))
            plot.showGrid(x=self.settings.show_grid, y=self.settings.show_grid,
                          alpha=self.settings.grid_alpha)
            plot.getAxis("left").enableAutoSIPrefix(self.settings.unit_mode == "auto")
        self._apply_pens()
        cross = pg.mkPen(t.text_muted, width=1, style=Qt.DashLine)
        for v in self._vlines:
            v.setPen(cross)
        for r in self._regions:
            accent = pg.mkColor(t.accent)
            fill = pg.mkColor(t.accent)
            fill.setAlpha(40)
            r.setBrush(pg.mkBrush(fill))
            for line in r.lines:
                line.setPen(pg.mkPen(accent, width=2))
                line.setHoverPen(pg.mkPen(accent, width=3))

    def _apply_pens(self) -> None:
        t = self.theme
        colors = {"v": t.chart_voltage, "i": t.chart_current, "p": t.chart_power}
        width = 1.0 if self._dense else self.settings.line_width
        # Antialiasing adds nothing visible on a dense min/max envelope but
        # costs several times the paint time.
        antialias = self.settings.antialias and not self._dense
        for key, curve in self._curves.items():
            curve.setPen(pg.mkPen(colors[key], width=width))
            curve.opts["antialias"] = antialias

    def set_theme(self, theme: ThemeColors) -> None:
        self.theme = theme
        self.apply_style()

    def apply_settings(self, settings) -> None:
        self.settings = settings
        visible = {"v": settings.show_voltage, "i": settings.show_current, "p": settings.show_power}
        if visible != self._visible:
            self._visible = visible
            self._build()
        else:
            for c in self._curves.values():
                c.opts["antialias"] = settings.antialias
            self.apply_style()
            self.refresh()

    def set_series_visible(self, key: str, visible: bool) -> None:
        if self._visible.get(key) == visible:
            return
        self._visible[key] = visible
        self._build()

    # ----------------------------------------------------------------- data

    def set_data(self, samples: Samples) -> None:
        self._data = samples

    def clear_data(self) -> None:
        # Not named clear(): GraphicsLayoutWidget binds `clear` to the layout's
        # clear() as an instance attribute, which would shadow it and wipe the scene.
        self._data = Samples.empty()
        for c in self._curves.values():
            c.setData([], [])
        self._region_values = None
        for r in self._regions:
            r.setVisible(False)
        self._set_x_range(0.0, self._window_s)
        if not self.overview:
            self.set_follow(True)

    def refresh(self) -> None:
        """Redraw the visible slice of the current data."""
        s = self._data
        if not len(s) or not self._plots:
            return
        t = s.t
        if self._follow and not self.overview:
            end = float(t[-1])
            self._set_x_range(max(0.0, end - self._window_s) if end > self._window_s else 0.0,
                              max(end, self._window_s))
        x0, x1 = self._first_plot().viewRange()[0]
        span = x1 - x0
        a = max(0, int(np.searchsorted(t, x0 - span * 0.02)) - 1)
        b = min(len(t), int(np.searchsorted(t, x1 + span * 0.02)) + 1)
        xs = t[a:b]
        width_px = max(200, int(self.width()))
        # Antialiased lines wider than 1 px cost up to seconds per repaint on
        # dense, noisy curves (Qt path stroking) and froze the window; once
        # there are more points than pixels a 1 px line looks the same.
        dense = (b - a) > 1.5 * width_px
        if dense != self._dense:
            self._dense = dense
            self._apply_pens()
        for key, curve in self._curves.items():
            curve.setData(*_decimate(xs, getattr(s, key)[a:b], width_px))

    def show_all(self) -> None:
        s = self._data
        if len(s) >= 2:
            t0, t1 = float(s.t[0]), float(s.t[-1])
            pad = (t1 - t0) * 0.01
            self._set_x_range(t0 - pad, t1 + pad)
        self.refresh()

    def zoom_to(self, t0: float, t1: float) -> None:
        if t1 > t0:
            self.set_follow(False)
            self._set_x_range(t0, t1)
            self.refresh()

    # ------------------------------------------------------- follow / window

    @property
    def follow(self) -> bool:
        return self._follow

    def set_follow(self, follow: bool) -> None:
        follow = bool(follow) and not self.overview
        if follow != self._follow:
            self._follow = follow
            self.follow_changed.emit(follow)
        if follow:
            self.refresh()

    @property
    def window_seconds(self) -> float:
        return self._window_s

    def set_window_seconds(self, seconds: float) -> None:
        self._window_s = float(min(max(seconds, self.MIN_WINDOW_S), self.MAX_WINDOW_S))
        if self._follow:
            self.refresh()
        else:
            x0, x1 = self._first_plot().viewRange()[0]
            c = (x0 + x1) / 2
            self._set_x_range(c - self._window_s / 2, c + self._window_s / 2)
            self.refresh()

    def _first_plot(self) -> pg.PlotItem:
        return next(iter(self._plots.values()))

    def _set_x_range(self, x0: float, x1: float) -> None:
        if not self._plots:
            return
        self._updating = True
        try:
            self._first_plot().setXRange(x0, x1, padding=0)
        finally:
            self._updating = False

    def _on_x_range_changed(self, *_):
        if self._updating:
            return
        # The user dragged the view: stop following the live edge.
        if self._follow:
            self._follow = False
            self.follow_changed.emit(False)
        self.refresh()
        self.view_changed.emit()

    # ---------------------------------------------------------------- mouse

    def wheelEvent(self, event) -> None:
        if not self._plots:
            return
        factor = 1 / self.ZOOM_FACTOR if event.angleDelta().y() > 0 else self.ZOOM_FACTOR
        if self._follow:
            self._window_s = float(min(max(self._window_s * factor, self.MIN_WINDOW_S),
                                       self.MAX_WINDOW_S))
            self.window_changed.emit(self._window_s)
            self.refresh()
        else:
            x0, x1 = self._first_plot().viewRange()[0]
            span = x1 - x0
            anchor, frac = (x0 + x1) / 2, 0.5
            pos = self.mapToScene(event.position().toPoint())
            for plot in self._plots.values():
                if plot.sceneBoundingRect().contains(pos):
                    anchor = plot.getViewBox().mapSceneToView(pos).x()
                    frac = (anchor - x0) / span if span > 0 else 0.5
                    break
            new_span = min(max(span * factor, self.MIN_WINDOW_S / 10), self.MAX_WINDOW_S * 24)
            start = anchor - frac * new_span
            self._set_x_range(start, start + new_span)
            if not self.overview:
                self._window_s = min(max(new_span, self.MIN_WINDOW_S), self.MAX_WINDOW_S)
                self.window_changed.emit(self._window_s)
            self.refresh()
            self.view_changed.emit()
        event.accept()

    # -------------------------------------------------------------- markers

    MARKER_PICK_PX = 8

    def set_markers(self, markers) -> None:
        self._markers = list(markers)
        self._draw_markers()

    def _draw_markers(self) -> None:
        for line in self._marker_lines:
            try:
                for plot in self._plots.values():
                    if line in plot.items:
                        plot.removeItem(line)
            except RuntimeError:
                pass
        self._marker_lines = []
        if not self._plots:
            return
        color = pg.mkColor(self.theme.warning)
        pen = pg.mkPen(color, width=1.5, style=Qt.DashLine)
        hover = pg.mkPen(color, width=3)
        first = True
        for plot in self._plots.values():
            for m in self._markers:
                opts = {}
                if first:
                    opts = dict(label=m.label, labelOpts={
                        "position": 0.9, "color": color, "anchors": [(-0.05, 0.5), (-0.05, 0.5)],
                        "fill": pg.mkBrush(pg.mkColor(self.theme.surface)),
                    })
                line = pg.InfiniteLine(pos=m.t, angle=90, movable=self.overview, pen=pen,
                                       hoverPen=hover, **opts)
                line.setZValue(20)
                line.marker_id = m.id
                if self.overview:
                    line.setCursor(Qt.SizeHorCursor)
                    line.sigPositionChanged.connect(self._sync_marker_drag)
                    line.sigPositionChangeFinished.connect(self._on_marker_dragged)
                plot.addItem(line, ignoreBounds=True)
                self._marker_lines.append(line)
            first = False

    def _sync_marker_drag(self, line) -> None:
        # Keep the copies of the same marker on the other plots aligned.
        x = line.value()
        for other in self._marker_lines:
            if other is not line and other.marker_id == line.marker_id and other.value() != x:
                other.blockSignals(True)
                other.setValue(x)
                other.blockSignals(False)

    def _on_marker_dragged(self, line) -> None:
        self.marker_moved.emit(int(line.marker_id), float(line.value()))

    def _time_at(self, scene_pos) -> Optional[Tuple[float, float]]:
        """(time, seconds per pixel) under a scene position, or None."""
        for plot in self._plots.values():
            if plot.sceneBoundingRect().contains(scene_pos):
                vb = plot.getViewBox()
                x = vb.mapSceneToView(scene_pos).x()
                x0, x1 = vb.viewRange()[0]
                width = max(1.0, vb.sceneBoundingRect().width())
                return x, (x1 - x0) / width
        return None

    def _marker_near(self, t: float, s_per_px: float) -> int:
        best, best_d = 0, self.MARKER_PICK_PX * s_per_px
        for m in self._markers:
            d = abs(m.t - t)
            if d <= best_d:
                best, best_d = m.id, d
        return best

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            hit = self._time_at(self.mapToScene(event.position().toPoint()))
            if hit is not None:
                t, spp = hit
                self.context_requested.emit(t, self._marker_near(t, spp),
                                            event.globalPosition().toPoint())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        hit = self._time_at(self.mapToScene(event.position().toPoint()))
        if hit is not None:
            mid = self._marker_near(*hit)
            if mid:
                self.marker_activated.emit(mid)
                event.accept()
                return
        if self.overview:
            self.show_all()
        else:
            self.set_follow(True)
        event.accept()

    def _on_mouse_moved(self, pos) -> None:
        if not self.settings.show_crosshair or not len(self._data):
            return
        for plot in self._plots.values():
            if plot.sceneBoundingRect().contains(pos):
                x = plot.getViewBox().mapSceneToView(pos).x()
                for v in self._vlines:
                    v.setPos(x)
                    v.setVisible(True)
                s = self._data
                k = int(np.clip(np.searchsorted(s.t, x), 1, len(s) - 1))
                if abs(s.t[k - 1] - x) < abs(s.t[k] - x):
                    k -= 1
                self.cursor_values.emit(float(s.t[k]), float(s.v[k]), float(s.i[k]), float(s.p[k]))
                return
        for v in self._vlines:
            v.setVisible(False)
        self.cursor_left.emit()

    def leaveEvent(self, event) -> None:
        for v in self._vlines:
            v.setVisible(False)
        self.cursor_left.emit()
        super().leaveEvent(event)

    # --------------------------------------------------------------- region

    def set_region(self, t0: float, t1: float) -> None:
        self._region_values = (min(t0, t1), max(t0, t1))
        self._syncing_region = True
        try:
            for r in self._regions:
                r.setRegion(self._region_values)
                r.setVisible(True)
        finally:
            self._syncing_region = False
        self.region_changed.emit()

    def get_selected_range(self) -> Optional[Tuple[float, float]]:
        return self._region_values

    def _on_region_moved(self, region: pg.LinearRegionItem) -> None:
        if self._syncing_region:
            return
        lo, hi = region.getRegion()
        self._region_values = (float(lo), float(hi))
        self._syncing_region = True
        try:
            for r in self._regions:
                if r is not region:
                    r.setRegion(self._region_values)
        finally:
            self._syncing_region = False
        self.region_changed.emit()
