"""Plot data buffers with timestamp management."""

from __future__ import annotations
from collections import deque
from typing import Deque


class PlotBuffers:
    """Manages plot data buffers with millisecond precision timestamps.
    
    Each measurement is stored as a separate point. Out-of-order or
    duplicate timestamps are discarded to prevent plot artifacts.
    
    Attributes:
        max_points: Maximum number of points to store (oldest are discarded)
        timestamps: Deque of Unix timestamps
        voltages: Deque of voltage values
        currents: Deque of current values
        powers: Deque of power values
    """
    
    def __init__(self, max_points: int = 5000):
        """Initialize buffers with given capacity.
        
        Args:
            max_points: Maximum number of data points to store
        """
        self.max_points = max_points
        self.timestamps: Deque[float] = deque(maxlen=max_points)
        self.voltages: Deque[float] = deque(maxlen=max_points)
        self.currents: Deque[float] = deque(maxlen=max_points)
        self.powers: Deque[float] = deque(maxlen=max_points)
    
    def append(self, t: float, v: float, i: float, p: float) -> None:
        """Append a sample. Discards if timestamp <= last (out of order).
        
        Args:
            t: Unix timestamp
            v: Voltage value
            i: Current value
            p: Power value
        """
        # Only accept strictly increasing timestamps
        if self.timestamps and t <= self.timestamps[-1]:
            return
        
        self.timestamps.append(t)
        self.voltages.append(v)
        self.currents.append(i)
        self.powers.append(p)
    
    def clear(self) -> None:
        """Clear all buffers."""
        self.timestamps.clear()
        self.voltages.clear()
        self.currents.clear()
        self.powers.clear()
    
    @property
    def is_empty(self) -> bool:
        """Check if buffers are empty."""
        return len(self.timestamps) == 0
    
    def __len__(self) -> int:
        """Return number of stored samples."""
        return len(self.timestamps)
