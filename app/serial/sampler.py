"""Sample rate control for data acquisition.

Handles subsampling when target rate is lower than device rate.
"""

from __future__ import annotations
import time
from typing import Optional


class SampleRateController:
    """Controls sampling rate with subsampling support."""
    
    def __init__(self, target_rate: int = 0, max_device_rate: int = 400):
        """Initialize sample rate controller.
        
        Args:
            target_rate: Target sampling rate in Hz (0 = no limit)
            max_device_rate: Maximum rate the device can provide
        """
        self.max_device_rate = max_device_rate
        self._last_sample_time: Optional[float] = None
        self._sample_count = 0
        self._start_time: Optional[float] = None

        # Rate matching state using time-based accumulator (robust to jitter)
        # Each arrival adds (elapsed * target_rate) tokens; consume 1 per accepted sample.
        self._accumulator = 0.0
        self._last_arrival: Optional[float] = None
        
        # Calculated target values (driven by requested target when subsampling)
        self._effective_target = 0.0
        self._needs_subsampling = False
        self._min_interval = 0.0

        self._set_target(target_rate)
    
    def should_accept_sample(self) -> bool:
        """Check if current sample should be accepted.
        
        Returns:
            True if sample should be accepted, False to skip (subsample)
        """
        if not self._needs_subsampling:
            return True  # Accept all samples
        
        now = time.perf_counter()

        if self._last_arrival is None:
            # First arrival: accept to establish timing
            self._last_arrival = now
            self._record_accept(now)
            return True

        interval = now - self._last_arrival
        self._last_arrival = now

        # Accumulate tokens proportional to elapsed time and target rate
        # Keep a soft cap to avoid huge bursts after long stalls, but allow
        # fractional carry to preserve accuracy (cap at 10 tokens).
        self._accumulator += interval * self._effective_target
        if self._accumulator > 10.0:
            self._accumulator = 10.0

        if self._accumulator >= 1.0:
            self._accumulator -= 1.0
            self._record_accept(now)
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
        self._accumulator = 0.0
        self._last_arrival = None
    
    def update_target(self, target_rate: int) -> None:
        """Update target sampling rate.
        
        Args:
            target_rate: New target rate in Hz (0 = no limit)
        """
        self._set_target(target_rate)
        self.reset()

    def _set_target(self, target_rate: int) -> None:
        """Clamp and store target parameters."""
        self.target_rate = target_rate

        # Simple contract:
        # - 0 => no limit (accept all)
        # - target > 0 => subsample to that target rate
        if target_rate <= 0:
            self._effective_target = 0.0
            self._needs_subsampling = False
            self._min_interval = 0.0
            return

        self._effective_target = float(target_rate)
        self._needs_subsampling = True
        self._min_interval = 1.0 / self._effective_target

    def _record_accept(self, now: float) -> None:
        """Update bookkeeping when a sample is accepted."""
        self._last_sample_time = now
        self._sample_count += 1
        if self._start_time is None:
            self._start_time = now
    
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
        if not self._needs_subsampling:
            return self.max_device_rate
        return int(self._effective_target)
