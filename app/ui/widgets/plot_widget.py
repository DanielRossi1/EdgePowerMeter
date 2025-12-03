"""Three-panel power monitoring plot widget with time window control."""

from __future__ import annotations
from typing import Optional, Tuple

import pyqtgraph as pg
from PySide6.QtCore import Qt

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
    """
    
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
        self._auto_scroll = True  # Follow live data
        self._last_data_time: float = 0.0
        self._is_panning = False
        
        self._setup_plots()
        self.region: Optional[pg.LinearRegionItem] = None
    
    def _setup_plots(self) -> None:
        """Create the three plot panels."""
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
        
        # Link X-axes so all plots pan together
        self.plot_i.setXLink(self.plot_v)
        self.plot_p.setXLink(self.plot_v)
        
        # Configure X-axis: we control range manually
        for plot in [self.plot_v, self.plot_i, self.plot_p]:
            plot.enableAutoRange(axis='x', enable=False)
            plot.setMouseEnabled(x=True, y=False)
            plot.enableAutoRange(axis='y', enable=True)
        
        # Connect to pan events (detect when user drags)
        self.plot_v.sigXRangeChanged.connect(self._on_x_range_changed)
    
    def _style_plot(self, plot: pg.PlotItem, color: str) -> None:
        """Apply theme styling to a plot panel."""
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.getAxis('left').setTextPen(self.theme.text_primary)
        plot.getAxis('left').setPen(self.theme.border_default)
        plot.getAxis('bottom').setTextPen(self.theme.text_primary)
        plot.getAxis('bottom').setPen(self.theme.border_default)
        plot.setTitle(plot.titleLabel.text, color=color, size='11pt')
        plot.getViewBox().setBackgroundColor(self.theme.bg_secondary)
    
    def _on_x_range_changed(self, view, range_) -> None:
        """Called when user pans the view."""
        if self._is_panning or not self._last_data_time:
            return
        
        # Check if we're near the live edge (within 0.5 seconds)
        view_end = range_[1]
        at_live_edge = (view_end >= self._last_data_time - 0.5)
        
        # If user dragged away from live edge, disable auto-scroll
        if not at_live_edge:
            self._auto_scroll = False
    
    def update_data(self, buffers: PlotBuffers) -> None:
        """Update plots with buffered data."""
        if buffers.is_empty:
            return
        
        # Get all data as numpy arrays
        xs, vs, cs, ps = buffers.get_arrays()
        
        if len(xs) == 0:
            return
        
        # Update curves with all data
        self.curve_v.setData(xs, vs)
        self.curve_i.setData(xs, cs)
        self.curve_p.setData(xs, ps)
        
        # Track latest data time
        self._last_data_time = xs[-1]
        
        # Update view window if auto-scrolling
        if self._auto_scroll:
            self._update_view_range()
    
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
            event.accept()
        else:
            super().mousePressEvent(event)
    
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
    
    def reset_to_live(self) -> None:
        """Reset to live auto-scrolling view (same as middle-click)."""
        self._auto_scroll = True
        self._update_view_range()
    
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
