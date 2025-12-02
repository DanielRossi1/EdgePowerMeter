"""Reusable UI widgets for EdgePowerMeter application.

This module provides modular, reusable UI components:

- StatCard: Value display card with color theming
- PlotWidget: Three-panel plot for V/I/P data with region selector
- PlotBuffers: Thread-safe data buffering for real-time plots
- PortDiscovery: Serial port detection and monitoring
"""

from .stat_card import StatCard
from .plot_widget import PlotWidget
from .plot_buffers import PlotBuffers
from .port_discovery import PortDiscovery

__all__ = ['StatCard', 'PlotWidget', 'PlotBuffers', 'PortDiscovery']
