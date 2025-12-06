"""Serial port discovery and monitoring utilities."""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from serial.tools import list_ports
from serial.tools.list_ports_common import ListPortInfo


class PortDiscovery:
    """Serial port discovery utility.
    
    Provides methods to list and filter available serial ports,
    with special handling for USB-to-serial devices commonly
    used with microcontrollers.
    """
    
    USB_MARKERS = ['USB', 'ACM', 'FTDI', 'CP210', 'CH340', 'PL2303']
    DEVICE_PATTERN = re.compile(r'ttyUSB|ttyACM|ttyAMA|cu\.usb|COM\d+', re.I)
    
    @classmethod
    def get_ports(cls, show_all: bool = False) -> List[Tuple[str, str]]:
        """Get list of available serial ports.
        
        Args:
            show_all: If True, returns all ports. If False, only USB devices.
            
        Returns:
            List of tuples (device_name, display_label)
        """
        result = []
        try:
            ports = list_ports.comports()
        except (TypeError, ValueError, OSError) as e:
            # Handle pyserial issues in sandboxed environments (snap/flatpak)
            print(f"[WARNING] Error listing serial ports: {e}")
            return result
        
        for port in ports:
            try:
                if show_all or cls._is_usb_device(port):
                    desc = port.description or port.hwid or 'Unknown'
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
        if getattr(port, 'vid', None) is not None:
            return True
        text = f"{port.description or ''} {port.hwid or ''}".upper()
        return any(m in text for m in cls.USB_MARKERS) or bool(cls.DEVICE_PATTERN.search(port.device))
    
    @staticmethod
    def list_ports() -> List[str]:
        """Get simple list of available serial port names.
        
        Returns:
            List of port device names (e.g., ['/dev/ttyUSB0', 'COM3'])
        """
        return [p.device for p in list_ports.comports()]
    
    @staticmethod
    def get_port_info(port_name: str) -> Optional[ListPortInfo]:
        """Get detailed info about a specific port.
        
        Args:
            port_name: Name of the port (e.g., '/dev/ttyUSB0')
            
        Returns:
            Port info object or None if not found
        """
        for p in list_ports.comports():
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
        for port in list_ports.comports():
            desc = (port.description or '').upper()
            if any(kw.upper() in desc for kw in esp32_keywords):
                result.append(port)
        return result
