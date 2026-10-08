"""Serial port configuration for EdgePowerMeter."""

from __future__ import annotations


class SerialConfig:
    """Configuration for serial port connection."""

    DEFAULT_BAUD = 2000000  # matches firmware SERIAL_BAUD (ESP32-C3 USB-CDC)
    BAUD_CHOICES = (115200, 230400, 460800, 921600, 1000000, 2000000)
