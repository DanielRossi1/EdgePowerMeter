"""Serial port discovery and monitoring utilities."""

from __future__ import annotations

import glob
import re
import sys
from typing import Any, List, Optional, Tuple

from serial.tools import list_ports
from serial.tools.list_ports_common import ListPortInfo


def _safe_attr(port: Any, name: str, default: Any = None) -> Any:
    """None-safe read of a ListPortInfo field.

    pyserial can leave USB-specific fields (vid, pid, serial_number, ...)
    unset or None when the underlying sysfs attributes aren't readable -
    e.g. under snap strict confinement without raw-usb/hardware-observe
    granted. Centralizes the None-coalescing so every call site is
    consistent and doesn't assume a field is always populated.
    """
    value = getattr(port, name, default)
    return value if value is not None else default


class PortDiscovery:
    """Serial port discovery utility.

    Provides methods to list and filter available serial ports,
    with special handling for USB-to-serial devices commonly
    used with microcontrollers.
    """

    USB_MARKERS = ['USB', 'ACM', 'FTDI', 'CP210', 'CH340', 'PL2303']
    DEVICE_PATTERN = re.compile(r'ttyUSB|ttyACM|ttyAMA|cu\.usb|COM\d+', re.I)

    # Mirrors the device globs pyserial's own Linux backend scans, used
    # only by the per-device fallback below.
    _DEVICE_GLOBS = (
        '/dev/ttyUSB*', '/dev/ttyACM*', '/dev/ttyXRUSB*',
        '/dev/ttyAMA*', '/dev/ttyAP*', '/dev/ttyS*', '/dev/rfcomm*',
    )

    @classmethod
    def _comports(cls) -> List[ListPortInfo]:
        """Enumerate serial ports, isolating per-device failures.

        pyserial's Linux backend builds every port's info inside a single
        list comprehension. If constructing any one device's info raises
        - e.g. `int(None)` on a sysfs attribute (bNumInterfaces) that
        can't be read under snap strict confinement without
        raw-usb/hardware-observe granted - the whole batch is lost,
        hiding perfectly good ports too. Fall back to scanning devices
        one at a time so a single bad port can't take the rest down, and
        keep permission-denied ports in the list (device path only)
        instead of dropping them silently. This holds even if the
        missing permissions are never granted.
        """
        try:
            return list(list_ports.comports())
        except (TypeError, ValueError, OSError) as e:
            print(f"[WARNING] Batch port enumeration failed ({e}); scanning devices individually")
        if not sys.platform.startswith('linux'):
            return []
        return cls._scan_devices_individually()

    @classmethod
    def _scan_devices_individually(cls) -> List[ListPortInfo]:
        """Linux-only fallback: build one ListPortInfo per device, isolated."""
        from serial.tools.list_ports_linux import SysFS

        devices: List[str] = []
        for pattern in cls._DEVICE_GLOBS:
            devices.extend(glob.glob(pattern))

        result = []
        for device in sorted(set(devices)):
            try:
                result.append(SysFS(device))
            except (TypeError, ValueError, OSError) as e:
                print(f"[WARNING] Could not read details for {device} ({e}); listing device path only")
                result.append(ListPortInfo(device))
        return result

    @classmethod
    def get_ports(cls, show_all: bool = False) -> List[Tuple[str, str]]:
        """Get list of available serial ports.

        Args:
            show_all: If True, returns all ports. If False, only USB devices.

        Returns:
            List of tuples (device_name, display_label)
        """
        result = []
        for port in cls._comports():
            try:
                if show_all or cls._is_usb_device(port):
                    desc = _safe_attr(port, 'description') or _safe_attr(port, 'hwid') or 'Unknown'
                    result.append((port.device, f"{port.device} — {desc}"))
            except (TypeError, ValueError, AttributeError) as e:
                # Skip ports that cause errors during enumeration
                print(f"[WARNING] Error processing port {getattr(port, 'device', 'unknown')}: {e}")
                continue
        return result

    @classmethod
    def _is_usb_device(cls, port: ListPortInfo) -> bool:
        """Check if port is a USB device.

        Args:
            port: Port info object

        Returns:
            True if port appears to be a USB serial device
        """
        if _safe_attr(port, 'vid') is not None:
            return True
        text = f"{_safe_attr(port, 'description', '')} {_safe_attr(port, 'hwid', '')}".upper()
        return any(m in text for m in cls.USB_MARKERS) or bool(cls.DEVICE_PATTERN.search(port.device))

    @classmethod
    def list_ports(cls) -> List[str]:
        """Get simple list of available serial port names.

        Returns:
            List of port device names (e.g., ['/dev/ttyUSB0', 'COM3'])
        """
        return [p.device for p in cls._comports()]

    @classmethod
    def get_port_info(cls, port_name: str) -> Optional[ListPortInfo]:
        """Get detailed info about a specific port.

        Args:
            port_name: Name of the port (e.g., '/dev/ttyUSB0')

        Returns:
            Port info object or None if not found
        """
        for p in cls._comports():
            if p.device == port_name:
                return p
        return None

    @classmethod
    def find_esp32_ports(cls) -> List[ListPortInfo]:
        """Find ports that appear to be ESP32 devices.

        Checks for common USB-UART bridge chips used with ESP32.

        Returns:
            List of port info objects for likely ESP32 devices
        """
        esp32_keywords = [
            'CP210',      # CP2102, CP2104
            'CH340',      # CH340G
            'CH910',      # CH910x
            'FTDI',       # FTDI chips
            'USB Serial', # Generic
            'USB-SERIAL', # Generic
            'ESP32',      # Direct ESP32
        ]

        result = []
        for port in cls._comports():
            desc = _safe_attr(port, 'description', '').upper()
            if any(kw.upper() in desc for kw in esp32_keywords):
                result.append(port)
        return result
