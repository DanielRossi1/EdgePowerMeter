"""Serial communication package for EdgePowerMeter."""

from .config import SerialConfig
from .handler import DeviceDisconnected, SerialPortHandler
from .protocol import Calibration, DeviceConfig, DeviceInfo, StreamDecoder
from .serial_reader import PROTO_LEGACY, PROTO_V2, SerialReader

__all__ = [
    "SerialConfig",
    "SerialPortHandler",
    "DeviceDisconnected",
    "Calibration",
    "DeviceConfig",
    "DeviceInfo",
    "StreamDecoder",
    "SerialReader",
    "PROTO_LEGACY",
    "PROTO_V2",
]
