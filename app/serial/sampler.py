"""Sample rate control for data acquisition.

Handles subsampling when target rate is lower than device rate.
"""

from __future__ import annotations
import time
from typing import Optional


class SampleRateController:
    """Controls sampling rate with subsampling support."""
    
    def __init__(self, target_rate: int = 0, max_device_rate: int = 100):
        """Initialize sample rate controller.
        
        Args:
            target_rate: Target sampling rate in Hz (0 = no limit)
            max_device_rate: Maximum rate the device can provide
        """
        self.target_rate = target_rate
        self.max_device_rate = max_device_rate
        self._last_sample_time: Optional[float] = None
        self._sample_count = 0
        self._start_time: Optional[float] = None
        
        # Calculate if subsampling is needed
        self._needs_subsampling = False
        self._min_interval = 0.0
        
        if target_rate > 0 and target_rate < max_device_rate:
            self._needs_subsampling = True
            self._min_interval = 1.0 / target_rate
    
    def should_accept_sample(self) -> bool:
        """Check if current sample should be accepted.
        
        Returns:
            True if sample should be accepted, False to skip (subsample)
        """
        if not self._needs_subsampling:
            return True  # Accept all samples
        
        current_time = time.perf_counter()
        
        if self._last_sample_time is None:
            self._last_sample_time = current_time
            if self._start_time is None:
                self._start_time = current_time
            self._sample_count += 1
            return True
        
        # Check if enough time has passed
        elapsed = current_time - self._last_sample_time
        if elapsed >= self._min_interval:
            self._last_sample_time = current_time
            self._sample_count += 1
            return True
        
        return False
    
    def get_actual_rate(self) -> float:
        """Get the actual sampling rate achieved.
        
        Returns:
            Actual rate in Hz, or 0 if not enough samples
        """
        if self._sample_count < 2 or self._start_time is None:
            return 0.0
        
        elapsed = time.perf_counter() - self._start_time
        if elapsed > 0:
            return self._sample_count / elapsed
        return 0.0
    
    def reset(self) -> None:
        """Reset the controller state."""
        self._last_sample_time = None
        self._sample_count = 0
        self._start_time = None
    
    def update_target(self, target_rate: int) -> None:
        """Update target sampling rate.
        
        Args:
            target_rate: New target rate in Hz (0 = no limit)
        """
        self.target_rate = target_rate
        
        # Recalculate subsampling parameters
        if target_rate > 0 and target_rate < self.max_device_rate:
            self._needs_subsampling = True
            self._min_interval = 1.0 / target_rate
        else:
            self._needs_subsampling = False
            self._min_interval = 0.0
        
        self.reset()
    
    @property
    def is_subsampling(self) -> bool:
        """Check if currently subsampling."""
        return self._needs_subsampling
    
    @property
    def effective_rate(self) -> int:
        """Get the effective sampling rate (what user will get).
        
        Returns:
            Effective rate in Hz
        """
        if self.target_rate == 0:
            return self.max_device_rate
        return min(self.target_rate, self.max_device_rate)
