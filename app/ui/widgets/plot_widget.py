"""Three-panel power monitoring plot widget with time window control."""

from __future__ import annotations
from typing import Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel
from PySide6.QtGui import QFont

# Enable OpenGL acceleration for smooth 60+ FPS rendering
_OPENGL_AVAILABLE = False
try:
    import OpenGL
    pg.setConfigOptions(useOpenGL=True, enableExperimental=True)
    _OPENGL_AVAILABLE = True
except ImportError:
    pass  # OpenGL not available, use software rendering

from ..theme import ThemeColors
from .plot_buffers import PlotBuffers


class PlotWidget(pg.GraphicsLayoutWidget):
    """Three-panel plot widget with sliding time window.
    
    Features:
        - Configurable time window (default 10 seconds)
        - Mouse wheel to zoom time window
        - Drag to pan through history
        - Auto-scroll when at live edge
        - Middle-click to reset to live view
        - All three plots synchronized
        - Crosshair with value display on hover
    """
    
    # Signal emitted when view needs refresh (pan, zoom, resize)
    view_changed = Signal()
    
    # Signal emitted when cursor hovers over data (t, v, i, p)
    cursor_values = Signal(float, float, float, float)
    
    # Time window settings
    DEFAULT_WINDOW_SECONDS = 10.0
    MIN_WINDOW_SECONDS = 1.0
    MAX_WINDOW_SECONDS = 300.0  # 5 minutes max
    ZOOM_FACTOR = 1.2
    
    def __init__(self, theme: ThemeColors, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.setBackground(theme.bg_secondary)
        
        # Time window state
        self._window_seconds = self.DEFAULT_WINDOW_SECONDS
        self._auto_scroll = True
        self._last_data_time = 0.0
        self._updating = False
        
        # Grid settings
        self._show_grid = True
        self._grid_alpha = 0.2
        
        # Crosshair settings
        self._show_crosshair = True
        self._crosshair_lines = []
        self._current_data = (np.array([]), np.array([]), np.array([]), np.array([]))
        
        self._setup_plots()
        self._setup_crosshair()
        self.region: Optional[pg.LinearRegionItem] = None
        
        # Enable mouse tracking for crosshair
        self.setMouseTracking(True)
    
    def _setup_plots(self) -> None:
        """Create the three plot panels."""
        # Enable antialiasing for smooth lines
        pg.setConfigOptions(antialias=True)
        
        # Voltage plot
        self.plot_v = self.addPlot(
            row=0, col=0, title="Voltage [V]"
        )
        self.plot_v.setLabel('bottom', 'Time [s]')
        self._style_plot(self.plot_v, self.theme.chart_voltage)
        self.curve_v = self.plot_v.plot(
            pen=pg.mkPen(self.theme.chart_voltage, width=2)
        )
        self.curve_v.setClipToView(True)  # Only render visible points
        
        self.nextRow()
        
        # Current plot
        self.plot_i = self.addPlot(
            row=1, col=0, title="Current [A]"
        )
        self.plot_i.setLabel('bottom', 'Time [s]')
        self._style_plot(self.plot_i, self.theme.chart_current)
        self.curve_i = self.plot_i.plot(
            pen=pg.mkPen(self.theme.chart_current, width=2)
        )
        self.curve_i.setClipToView(True)
        
        self.nextRow()
        
        # Power plot
        self.plot_p = self.addPlot(
            row=2, col=0, title="Power [W]"
        )
        self.plot_p.setLabel('bottom', 'Time [s]')
        self._style_plot(self.plot_p, self.theme.chart_power)
        self.curve_p = self.plot_p.plot(
            pen=pg.mkPen(self.theme.chart_power, width=2)
        )
        self.curve_p.setClipToView(True)
        
        # Link X-axes so all plots pan together
        self.plot_i.setXLink(self.plot_v)
        self.plot_p.setXLink(self.plot_v)
        
        # Configure X-axis: we control range manually
        for plot in [self.plot_v, self.plot_i, self.plot_p]:
            plot.enableAutoRange(axis='x', enable=False)
            plot.setMouseEnabled(x=True, y=False)
            plot.enableAutoRange(axis='y', enable=True)
        
        # Connect to ViewBox signal for pan/zoom detection
        self.plot_v.getViewBox().sigRangeChanged.connect(self._on_view_changed)
    
    def _setup_crosshair(self) -> None:
        """Setup crosshair lines for value display on hover."""
        # Vertical line that spans all plots (synced via X-link)
        pen = pg.mkPen(color=self.theme.text_muted, width=1, style=Qt.DashLine)
        
        for plot in [self.plot_v, self.plot_i, self.plot_p]:
            vline = pg.InfiniteLine(angle=90, movable=False, pen=pen)
            vline.setVisible(False)
            plot.addItem(vline, ignoreBounds=True)
            self._crosshair_lines.append(vline)
        
        # Connect mouse move signal from each plot
        for plot in [self.plot_v, self.plot_i, self.plot_p]:
            plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
    
    def _on_mouse_moved(self, pos) -> None:
        """Handle mouse move for crosshair."""
        if not self._show_crosshair:
            return
        
        # Check if position is within any plot
        for plot in [self.plot_v, self.plot_i, self.plot_p]:
            if plot.sceneBoundingRect().contains(pos):
                mouse_point = plot.getViewBox().mapSceneToView(pos)
                x = mouse_point.x()
                
                # Show crosshair lines
                for vline in self._crosshair_lines:
                    vline.setPos(x)
                    vline.setVisible(True)
                
                # Find nearest data point and emit values
                self._emit_cursor_values(x)
                return
        
        # Mouse outside plots - hide crosshair
        for vline in self._crosshair_lines:
            vline.setVisible(False)
    
    def _emit_cursor_values(self, x: float) -> None:
        """Find and emit values at cursor position."""
        xs, vs, cs, ps = self._current_data
        if len(xs) == 0:
            return
        
        # Find nearest index using binary search
        idx = np.searchsorted(xs, x)
        if idx >= len(xs):
            idx = len(xs) - 1
        elif idx > 0:
            # Check which neighbor is closer
            if abs(xs[idx-1] - x) < abs(xs[idx] - x):
                idx = idx - 1
        
        self.cursor_values.emit(xs[idx], vs[idx], cs[idx], ps[idx])
    
    def _style_plot(self, plot: pg.PlotItem, color: str) -> None:
        """Apply theme styling to a plot panel."""
        plot.showGrid(x=self._show_grid, y=self._show_grid, alpha=self._grid_alpha)
        plot.getAxis('left').setTextPen(self.theme.text_primary)
        plot.getAxis('left').setPen(self.theme.border_default)
        plot.getAxis('bottom').setTextPen(self.theme.text_primary)
        plot.getAxis('bottom').setPen(self.theme.border_default)
        plot.setTitle(plot.titleLabel.text, color=color, size='11pt')
        plot.getViewBox().setBackgroundColor(self.theme.bg_secondary)
    
    def _on_view_changed(self, vb, range_) -> None:
        """Called when ViewBox range changes (user pan/zoom)."""
        if self._updating:
            return
            
        # Check if near live edge
        x_range = range_[0]
        if self._last_data_time > 0:
            at_live_edge = x_range[1] >= self._last_data_time - 0.5
            if not at_live_edge:
                self._auto_scroll = False
        
        self.view_changed.emit()
    
    def update_data(self, buffers: PlotBuffers) -> None:
        """Update plots with visible portion of data."""
        if buffers.is_empty:
            return
        
        xs, vs, cs, ps = buffers.get_arrays()
        if len(xs) == 0:
            return
        
        self._updating = True
        
        # Store data for crosshair lookup
        self._current_data = (xs, vs, cs, ps)
        
        self._last_data_time = xs[-1]
        
        # If auto-scroll, update view range to follow live data
        if self._auto_scroll:
            t_end = self._last_data_time
            t_start = t_end - self._window_seconds
            self.plot_v.setXRange(t_start, t_end, padding=0)
        
        # Get visible range and render only that portion
        view_range = self.plot_v.viewRange()[0]
        t_start = view_range[0] - 0.5
        t_end = view_range[1] + 0.5
        
        # Find visible slice
        idx_start = max(0, np.searchsorted(xs, t_start) - 1)
        idx_end = min(len(xs), np.searchsorted(xs, t_end) + 1)
        
        # Update curves with only visible data
        self.curve_v.setData(xs[idx_start:idx_end], vs[idx_start:idx_end])
        self.curve_i.setData(xs[idx_start:idx_end], cs[idx_start:idx_end])
        self.curve_p.setData(xs[idx_start:idx_end], ps[idx_start:idx_end])
        
        self._updating = False
    
    def _update_view_range(self) -> None:
        """Update the visible time range to follow live data."""
        if self._last_data_time <= 0:
            return
        
        # Follow live data: show last N seconds
        t_end = self._last_data_time
        t_start = t_end - self._window_seconds
        
        # Prevent signal recursion
        self._is_panning = True
        self.plot_v.setXRange(t_start, t_end, padding=0)
        self._is_panning = False
    
    def wheelEvent(self, event) -> None:
        """Handle mouse wheel for time window zoom."""
        delta = event.angleDelta().y()
        
        if delta > 0:
            # Zoom in (smaller window)
            self._window_seconds = max(
                self.MIN_WINDOW_SECONDS,
                self._window_seconds / self.ZOOM_FACTOR
            )
        else:
            # Zoom out (larger window)
            self._window_seconds = min(
                self.MAX_WINDOW_SECONDS,
                self._window_seconds * self.ZOOM_FACTOR
            )
        
        # Apply new window
        if self._auto_scroll:
            self._update_view_range()
        else:
            # Zoom around center of current view
            current_range = self.plot_v.viewRange()[0]
            center = (current_range[0] + current_range[1]) / 2
            t_start = center - self._window_seconds / 2
            t_end = center + self._window_seconds / 2
            
            self._is_panning = True
            self.plot_v.setXRange(t_start, t_end, padding=0)
            self._is_panning = False
        
        event.accept()
    
    def mousePressEvent(self, event) -> None:
        """Handle mouse press events."""
        if event.button() == Qt.MiddleButton:
            # Reset to live view
            self._auto_scroll = True
            self._window_seconds = self.DEFAULT_WINDOW_SECONDS
            self._update_view_range()
            self.view_changed.emit()
            event.accept()
        else:
            super().mousePressEvent(event)
    
    def mouseReleaseEvent(self, event) -> None:
        """Handle mouse release - ensure final update after drag."""
        super().mouseReleaseEvent(event)
        # Force update when user finishes dragging
        self.view_changed.emit()
    
    def resizeEvent(self, event) -> None:
        """Handle resize events - trigger re-render."""
        super().resizeEvent(event)
        self.view_changed.emit()
    
    # -------------------------------------------------------------------------
    # Region selector for data export
    # -------------------------------------------------------------------------
    
    def add_region_selector(self, t_min: float, t_max: float) -> None:
        """Add or update a region selector on the power plot."""
        self.remove_region_selector()
        self.region = pg.LinearRegionItem(
            values=(t_min, t_max),
            brush=pg.mkBrush(self.theme.accent_primary + "30"),
            pen=pg.mkPen(self.theme.accent_primary, width=2),
        )
        self.region.setZValue(10)
        self.plot_p.addItem(self.region)
    
    def remove_region_selector(self) -> None:
        """Remove the region selector if present."""
        if self.region:
            try:
                self.plot_p.removeItem(self.region)
            except Exception:
                pass
            self.region = None
    
    def get_selected_range(self) -> Optional[Tuple[float, float]]:
        """Get the currently selected time range."""
        if not self.region:
            return None
        return tuple(sorted(self.region.getRegion()))
    
    # -------------------------------------------------------------------------
    # Theme and cleanup
    # -------------------------------------------------------------------------
    
    def update_theme(self, theme: ThemeColors) -> None:
        """Update all theme-dependent colors."""
        self.theme = theme
        self.setBackground(theme.bg_secondary)
        
        for plot, color in [
            (self.plot_v, theme.chart_voltage),
            (self.plot_i, theme.chart_current),
            (self.plot_p, theme.chart_power)
        ]:
            plot.getAxis('left').setTextPen(theme.text_primary)
            plot.getAxis('left').setPen(theme.border_default)
            plot.getAxis('bottom').setTextPen(theme.text_primary)
            plot.getAxis('bottom').setPen(theme.border_default)
            plot.setTitle(plot.titleLabel.text, color=color, size='11pt')
            plot.getViewBox().setBackgroundColor(theme.bg_secondary)
        
        self.curve_v.setPen(pg.mkPen(theme.chart_voltage, width=2))
        self.curve_i.setPen(pg.mkPen(theme.chart_current, width=2))
        self.curve_p.setPen(pg.mkPen(theme.chart_power, width=2))
        
        if self.region:
            self.region.setBrush(pg.mkBrush(theme.accent_primary + "30"))
            self.region.setPen(pg.mkPen(theme.accent_primary, width=2))
    
    def clear_data(self) -> None:
        """Clear all plot data and reset view."""
        self.curve_v.setData([], [])
        self.curve_i.setData([], [])
        self.curve_p.setData([], [])
        self.remove_region_selector()
        
        # Reset state
        self._auto_scroll = True
        self._window_seconds = self.DEFAULT_WINDOW_SECONDS
        self._last_data_time = 0.0
        
        # Reset X range to start from 0
        self._is_panning = True
        self.plot_v.setXRange(0, self._window_seconds, padding=0)
        self._is_panning = False
        
        # Clear crosshair data
        self._current_data = (np.array([]), np.array([]), np.array([]), np.array([]))
    
    def reset_to_live(self) -> None:
        """Reset to live auto-scrolling view (same as middle-click)."""
        self._auto_scroll = True
        self._update_view_range()
    
    def show_full_range(self, t_min: float, t_max: float) -> None:
        """Show the full time range (for export view).
        
        Disables auto-scroll and sets view to show all data.
        """
        self._auto_scroll = False
        duration = t_max - t_min
        self._window_seconds = max(duration, self.MIN_WINDOW_SECONDS)
        
        # Add small padding
        padding = duration * 0.02 if duration > 0 else 0.5
        
        self._is_panning = True
        self.plot_v.setXRange(t_min - padding, t_max + padding, padding=0)
        self._is_panning = False
    
    # -------------------------------------------------------------------------
    # Settings configuration
    # -------------------------------------------------------------------------
    
    def set_grid(self, show: bool, alpha: float = 0.2) -> None:
        """Configure grid visibility and opacity."""
        self._show_grid = show
        self._grid_alpha = alpha
        for plot in [self.plot_v, self.plot_i, self.plot_p]:
            plot.showGrid(x=show, y=show, alpha=alpha)
    
    def set_crosshair(self, show: bool) -> None:
        """Enable/disable crosshair cursor."""
        self._show_crosshair = show
        if not show:
            for vline in self._crosshair_lines:
                vline.setVisible(False)
    
    @property
    def window_seconds(self) -> float:
        """Current time window size in seconds."""
        return self._window_seconds
    
    @window_seconds.setter
    def window_seconds(self, value: float) -> None:
        """Set time window size."""
        self._window_seconds = max(
            self.MIN_WINDOW_SECONDS,
            min(self.MAX_WINDOW_SECONDS, value)
        )
        if self._auto_scroll:
            self._update_view_range()
