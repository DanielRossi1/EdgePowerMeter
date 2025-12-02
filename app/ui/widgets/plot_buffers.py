"""Plot data buffers optimized for real-time time series."""

from __future__ import annotations
from typing import List, Tuple
import numpy as np


class PlotBuffers:
    """High-performance buffer for real-time plotting.
    
    Uses Python lists for O(1) append. All data is stored and accessible.
    The PlotWidget handles the view window (what portion to display).
    
    Strategy:
    - Collect data in Python lists (fast append)
    - All data remains accessible for export
    - PlotWidget controls what range to show
    """
    
    def __init__(self, max_display_points: int = 5000):
        """Initialize buffers.
        
        Args:
            max_display_points: Unused, kept for compatibility
        """
        self.max_display_points = max_display_points
        
        # Use Python lists for O(1) append
        self._timestamps: List[float] = []
        self._voltages: List[float] = []
        self._currents: List[float] = []
        self._powers: List[float] = []
        
        # Cached numpy arrays (invalidated on append)
        self._cache_valid = False
        self._np_ts: np.ndarray = np.array([])
        self._np_v: np.ndarray = np.array([])
        self._np_i: np.ndarray = np.array([])
        self._np_p: np.ndarray = np.array([])
    
    def append(self, t: float, v: float, i: float, p: float) -> None:
        """Append a sample. O(1) operation."""
        if self._timestamps and t <= self._timestamps[-1]:
            return
        
        self._timestamps.append(t)
        self._voltages.append(v)
        self._currents.append(i)
        self._powers.append(p)
        self._cache_valid = False
    
    def clear(self) -> None:
        """Clear all buffers."""
        self._timestamps.clear()
        self._voltages.clear()
        self._currents.clear()
        self._powers.clear()
        self._cache_valid = False
        self._np_ts = np.array([])
        self._np_v = np.array([])
        self._np_i = np.array([])
        self._np_p = np.array([])
    
    @property
    def is_empty(self) -> bool:
        return len(self._timestamps) == 0
    
    def __len__(self) -> int:
        return len(self._timestamps)
    
    def _ensure_cache(self) -> None:
        """Build numpy cache if needed."""
        if self._cache_valid:
            return
        self._np_ts = np.array(self._timestamps, dtype=np.float64)
        self._np_v = np.array(self._voltages, dtype=np.float64)
        self._np_i = np.array(self._currents, dtype=np.float64)
        self._np_p = np.array(self._powers, dtype=np.float64)
        self._cache_valid = True
    
    def get_arrays(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Get all data as numpy arrays."""
        self._ensure_cache()
        return self._np_ts, self._np_v, self._np_i, self._np_p
    
    def get_time_range(self) -> Tuple[float, float]:
        """Get min and max timestamp."""
        if not self._timestamps:
            return (0.0, 0.0)
        return (self._timestamps[0], self._timestamps[-1])
    
    def get_latest_time(self) -> float:
        """Get most recent timestamp."""
        return self._timestamps[-1] if self._timestamps else 0.0
    
    # Compatibility properties
    @property
    def timestamps(self) -> List[float]:
        return self._timestamps
    
    @property
    def voltages(self) -> List[float]:
        return self._voltages
    
    @property
    def currents(self) -> List[float]:
        return self._currents
    
    @property
    def powers(self) -> List[float]:
        return self._powers
    
    @property 
    def max_points(self) -> int:
        return self.max_display_points
    
    @max_points.setter
    def max_points(self, value: int) -> None:
        self.max_display_points = value
