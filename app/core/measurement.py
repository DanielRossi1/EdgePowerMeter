"""Measurement data structures."""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Measurement:
    """Raw measurement from serial port."""
    timestamp: datetime
    voltage: float
    current: float
    power: float
    
    def to_dict(self) -> dict:
        """Convert measurement to dictionary format."""
        return {
            'timestamp': self.timestamp,
            'voltage': self.voltage,
            'current': self.current,
            'power': self.power,
        }
    
    def __str__(self) -> str:
        return (
            f"Measurement("
            f"t={self.timestamp.strftime('%H:%M:%S.%f')[:-3]}, "
            f"V={self.voltage:.4f}, "
            f"I={self.current:.4f}, "
            f"P={self.power:.4f})"
        )


@dataclass
class MeasurementRecord:
    """Measurement record with timing information for storage and export."""
    timestamp: datetime
    unix_time: float
    relative_time: float  # Time from acquisition start (seconds)
    voltage: float
    current: float
    power: float
