"""Three-panel power monitoring plot widget."""

from __future__ import annotations
from typing import Optional, Tuple

import pyqtgraph as pg
from pyqtgraph.graphicsItems.DateAxisItem import DateAxisItem
from PySide6.QtCore import Qt

from ..theme import ThemeColors
from .plot_buffers import PlotBuffers


class PlotWidget(pg.GraphicsLayoutWidget):
    """Three-panel plot widget for voltage, current, and power.
    
    Features:
        - Independent zoom for each panel
        - Time axis with date/time formatting
        - Theme support (dark/light mode)
        - Region selector for data selection
        - Middle-click to reset auto-scroll on X-axis
    """
    
    def __init__(self, theme: ThemeColors, parent=None):
        """Initialize plot widget.
        
        Args:
            theme: Theme colors to use
            parent: Parent widget
        """
        super().__init__(parent)
        self.theme = theme
        self.setBackground(theme.bg_secondary)
        self._setup_plots()
        self.region: Optional[pg.LinearRegionItem] = None
    
    def _setup_plots(self) -> None:
        """Create the three plot panels."""
        pg.setConfigOptions(antialias=True)
        
        # Voltage plot
        self.plot_v = self.addPlot(
            row=0, col=0, title="Voltage [V]",
            axisItems={'bottom': DateAxisItem(orientation='bottom')}
        )
        self._style_plot(self.plot_v, self.theme.chart_voltage)
        self.curve_v = self.plot_v.plot(
            pen=pg.mkPen(self.theme.chart_voltage, width=2)
        )
        
        self.nextRow()
        
        # Current plot
        self.plot_i = self.addPlot(
            row=1, col=0, title="Current [A]",
            axisItems={'bottom': DateAxisItem(orientation='bottom')}
        )
        self._style_plot(self.plot_i, self.theme.chart_current)
        self.curve_i = self.plot_i.plot(
            pen=pg.mkPen(self.theme.chart_current, width=2)
        )
        
        self.nextRow()
        
        # Power plot
        self.plot_p = self.addPlot(
            row=2, col=0, title="Power [W]",
            axisItems={'bottom': DateAxisItem(orientation='bottom')}
        )
        self._style_plot(self.plot_p, self.theme.chart_power)
        self.curve_p = self.plot_p.plot(
            pen=pg.mkPen(self.theme.chart_power, width=2)
        )
    
    def _style_plot(self, plot: pg.PlotItem, color: str) -> None:
        """Apply theme styling to a plot panel."""
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.getAxis('left').setTextPen(self.theme.text_primary)
        plot.getAxis('left').setPen(self.theme.border_default)
        plot.getAxis('bottom').setTextPen(self.theme.text_primary)
        plot.getAxis('bottom').setPen(self.theme.border_default)
        plot.setTitle(plot.titleLabel.text, color=color, size='11pt')
        plot.getViewBox().setBackgroundColor(self.theme.bg_secondary)
        
        # Lock Y-axis: only allow zoom/pan on X-axis
        # Y range will auto-scale to visible data
        plot.getViewBox().setMouseEnabled(x=True, y=False)
        plot.enableAutoRange(axis='y', enable=True)
    
    def update_data(self, buffers: PlotBuffers) -> None:
        """Update plots with new data.
        
        Args:
            buffers: PlotBuffers containing the data to display
        """
        if buffers.is_empty:
            return
        xs = list(buffers.timestamps)
        self.curve_v.setData(xs, list(buffers.voltages))
        self.curve_i.setData(xs, list(buffers.currents))
        self.curve_p.setData(xs, list(buffers.powers))
    
    def add_region_selector(self, t_min: float, t_max: float) -> None:
        """Add or update a region selector on the power plot.
        
        Args:
            t_min: Start time (Unix timestamp)
            t_max: End time (Unix timestamp)
        """
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
        """Get the currently selected time range.
        
        Returns:
            Tuple of (start_time, end_time) or None if no selection
        """
        if not self.region:
            return None
        return tuple(sorted(self.region.getRegion()))
    
    def update_theme(self, theme: ThemeColors) -> None:
        """Update all theme-dependent colors.
        
        Args:
            theme: New theme colors to apply
        """
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
        """Clear all plot data."""
        self.curve_v.setData([], [])
        self.curve_i.setData([], [])
        self.curve_p.setData([], [])
        self.remove_region_selector()
        # Reset auto-range after clearing
        self.reset_x_autorange()
    
    def mousePressEvent(self, event) -> None:
        """Handle mouse press events.
        
        Middle-click resets X-axis auto-range for all plots.
        """
        if event.button() == Qt.MiddleButton:
            self.reset_x_autorange()
        super().mousePressEvent(event)
    
    def reset_x_autorange(self) -> None:
        """Reset X-axis to auto-range mode for all plots.
        
        Call this to resume automatic scrolling after manual zoom/pan.
        """
        for plot in [self.plot_v, self.plot_i, self.plot_p]:
            plot.enableAutoRange(axis='x', enable=True)
