"""Serial port configuration for EdgePowerMeter."""

from __future__ import annotations


class SerialConfig:
    """Configuration for serial port connection."""
    DEFAULT_BAUD = 921600  # High-speed for ESP32-C3 USB-CDC
    DEFAULT_TIMEOUT = 1.0
    VTIME_DECISECONDS = 10  # 1 second timeout in deciseconds
    
    TIMESTAMP_FORMATS = [
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S.%f',
    ]
