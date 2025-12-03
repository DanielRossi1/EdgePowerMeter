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
    - Relative time (from 0) is used for plotting
    - Absolute timestamps are preserved for export
    """
    
    def __init__(self, max_display_points: int = 5000):
        """Initialize buffers.
        
        Args:
            max_display_points: Unused, kept for compatibility
        """
        self.max_display_points = max_display_points
        
        # Use Python lists for O(1) append
        self._timestamps: List[float] = []  # Absolute timestamps (unix time)
        self._relative_times: List[float] = []  # Relative time from start (seconds)
        self._voltages: List[float] = []
        self._currents: List[float] = []
        self._powers: List[float] = []
        
        # Track acquisition start time
        self._start_time: float = 0.0
        
        # Cached numpy arrays (invalidated on append)
        self._cache_valid = False
        self._np_ts: np.ndarray = np.array([])  # Absolute timestamps
        self._np_rel: np.ndarray = np.array([])  # Relative times
        self._np_v: np.ndarray = np.array([])
        self._np_i: np.ndarray = np.array([])
        self._np_p: np.ndarray = np.array([])
    
    def append(self, t: float, v: float, i: float, p: float) -> None:
        """Append a sample. O(1) operation."""
        if self._timestamps and t <= self._timestamps[-1]:
            return
        
        # Set start time on first sample
        if not self._timestamps:
            self._start_time = t
        
        self._timestamps.append(t)
        self._relative_times.append(t - self._start_time)
        self._voltages.append(v)
        self._currents.append(i)
        self._powers.append(p)
        self._cache_valid = False
    
    def clear(self) -> None:
        """Clear all buffers."""
        self._timestamps.clear()
        self._relative_times.clear()
        self._voltages.clear()
        self._currents.clear()
        self._powers.clear()
        self._start_time = 0.0
        self._cache_valid = False
        self._np_ts = np.array([])
        self._np_rel = np.array([])
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
        self._np_rel = np.array(self._relative_times, dtype=np.float64)
        self._np_v = np.array(self._voltages, dtype=np.float64)
        self._np_i = np.array(self._currents, dtype=np.float64)
        self._np_p = np.array(self._powers, dtype=np.float64)
        self._cache_valid = True
    
    def get_arrays(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Get data as numpy arrays with relative time for plotting."""
        self._ensure_cache()
        return self._np_rel, self._np_v, self._np_i, self._np_p
    
    def get_arrays_absolute(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Get data as numpy arrays with absolute timestamps."""
        self._ensure_cache()
        return self._np_ts, self._np_v, self._np_i, self._np_p
    
    def get_time_range(self) -> Tuple[float, float]:
        """Get min and max relative time (for plotting)."""
        if not self._relative_times:
            return (0.0, 0.0)
        return (self._relative_times[0], self._relative_times[-1])
    
    def get_absolute_time_range(self) -> Tuple[float, float]:
        """Get min and max absolute timestamp (for export)."""
        if not self._timestamps:
            return (0.0, 0.0)
        return (self._timestamps[0], self._timestamps[-1])
    
    def get_latest_time(self) -> float:
        """Get most recent relative time."""
        return self._relative_times[-1] if self._relative_times else 0.0
    
    def get_start_time(self) -> float:
        """Get acquisition start time (absolute timestamp)."""
        return self._start_time
    
    def relative_to_absolute(self, rel_time: float) -> float:
        """Convert relative time to absolute timestamp."""
        return self._start_time + rel_time
    
    def absolute_to_relative(self, abs_time: float) -> float:
        """Convert absolute timestamp to relative time."""
        return abs_time - self._start_time
    
    # Compatibility properties
    @property
    def timestamps(self) -> List[float]:
        """Absolute timestamps (for export compatibility)."""
        return self._timestamps
    
    @property
    def relative_times(self) -> List[float]:
        """Relative times from start (for plotting)."""
        return self._relative_times
    
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
