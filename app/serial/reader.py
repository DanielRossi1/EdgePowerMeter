"""Serial port reader for EdgePowerMeter.

This module provides a thread-safe serial reader that parses measurement data
from the EdgePowerMeter firmware and emits it as Qt signals.
"""

from __future__ import annotations

import io
import fcntl
import logging
import os
import termios
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from PySide6 import QtCore
import serial

logger = logging.getLogger(__name__)


@dataclass
class Measurement:
    """Represents a single power measurement from the device."""
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


class SerialConfig:
    """Configuration for serial port connection."""
    DEFAULT_BAUD = 115200
    DEFAULT_TIMEOUT = 1.0
    VTIME_DECISECONDS = 10  # 1 second timeout in deciseconds
    
    TIMESTAMP_FORMATS = [
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S.%f',
    ]


class MeasurementParser:
    """Parser for measurement data from serial port."""
    
    # Timestamp before this year is considered invalid (RTC not set)
    MIN_VALID_YEAR = 2020
    
    @staticmethod
    def parse_timestamp(ts_str: str) -> datetime:
        """Parse timestamp string from firmware.
        
        Args:
            ts_str: Timestamp string in format 'YYYY-MM-DD HH:MM:SS'
            
        Returns:
            Parsed datetime, or current time if RTC timestamp is invalid.
        """
        ts_str = ts_str.strip()
        
        for fmt in SerialConfig.TIMESTAMP_FORMATS:
            try:
                parsed = datetime.strptime(ts_str, fmt)
                # If year is before 2020, RTC is not set - use local time
                if parsed.year < MeasurementParser.MIN_VALID_YEAR:
                    return datetime.now()
                return parsed
            except ValueError:
                continue
        
        return datetime.now()

    @staticmethod
    def parse_csv_line(line: str) -> Optional[Measurement]:
        """Parse CSV format: 'Timestamp,Voltage,Current,Power'."""
        parts = [p.strip() for p in line.split(',')]
        
        if len(parts) < 4:
            return None
        
        try:
            timestamp = MeasurementParser.parse_timestamp(parts[0])
            voltage = float(parts[1])
            current = float(parts[2])
            power = float(parts[3])
            return Measurement(timestamp, voltage, current, power)
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_space_separated(line: str) -> Optional[Measurement]:
        """Parse space-separated format: 'Voltage Current Power'."""
        parts = line.split()
        
        if len(parts) < 3:
            return None
        
        try:
            voltage = float(parts[0])
            current = float(parts[1])
            power = float(parts[2])
            return Measurement(datetime.now(), voltage, current, power)
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_line(line: str) -> Optional[Measurement]:
        """Parse a line in any supported format.
        
        Supports:
            - CSV with timestamp: '2025-11-30 12:34:56,12.345,1.234,15.234'
            - Space-separated: '12.345 1.234 15.234'
        """
        if not line:
            return None
        
        if ',' in line:
            return MeasurementParser.parse_csv_line(line)
        else:
            return MeasurementParser.parse_space_separated(line)


class SerialPortHandler:
    """Handles low-level serial port operations."""
    
    def __init__(self, port: str, baud: int = SerialConfig.DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self._fd: Optional[int] = None
        self._file: Optional[io.TextIOWrapper] = None
        self._ser: Optional[serial.Serial] = None
        self._use_direct = False

    @property
    def is_open(self) -> bool:
        """Check if port is open."""
        if self._use_direct:
            return self._fd is not None and self._file is not None
        return self._ser is not None and self._ser.is_open

    def open(self) -> None:
        """Open serial port, trying direct method first, then pyserial."""
        try:
            self._open_direct()
            self._use_direct = True
            logger.info(f"Opened {self.port} using direct file descriptor")
        except Exception as e1:
            logger.warning(f"Direct open failed: {e1}, trying pyserial...")
            try:
                self._open_pyserial()
                self._use_direct = False
                logger.info(f"Opened {self.port} using pyserial")
            except Exception as e2:
                raise ConnectionError(
                    f"Impossibile aprire {self.port}:\n"
                    f"  Metodo diretto: {e1}\n"
                    f"  PySerial: {e2}\n"
                    "Controlla che il device esista e i permessi siano corretti."
                ) from e2

    def _open_direct(self) -> None:
        """Open serial port using direct file descriptor (Linux)."""
        self._fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        
        try:
            attrs = termios.tcgetattr(self._fd)
            baud_constant = getattr(termios, f'B{self.baud}', termios.B115200)
            
            # Set baud rate
            attrs[4] = baud_constant  # ispeed
            attrs[5] = baud_constant  # ospeed
            
            # Configure for raw mode (8N1)
            attrs[0] = 0  # iflag
            attrs[1] = 0  # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
            attrs[3] = 0  # lflag
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = SerialConfig.VTIME_DECISECONDS
            
            termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
            
            # Clear non-blocking flag
            flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)
            fcntl.fcntl(self._fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            
            # Flush buffers
            termios.tcflush(self._fd, termios.TCIOFLUSH)
            
            # Create file wrapper
            self._file = io.TextIOWrapper(
                io.FileIO(self._fd, mode='rb', closefd=False),
                encoding='utf-8',
                errors='ignore',
                newline='\n'
            )
        except Exception:
            self._close_fd()
            raise

    def _open_pyserial(self) -> None:
        """Open serial port using pyserial."""
        self._ser = serial.Serial(
            self.port,
            self.baud,
            timeout=SerialConfig.DEFAULT_TIMEOUT
        )
        time.sleep(0.1)  # Let port stabilize
        self._ser.reset_input_buffer()  # Flush any old data

    def readline(self) -> str:
        """Read a line from serial port."""
        if self._use_direct and self._file:
            line = self._file.readline()
            return line.strip() if line else ""
        elif self._ser:
            raw = self._ser.readline()
            return raw.decode(errors='ignore').strip() if raw else ""
        return ""

    def close(self) -> None:
        """Close all serial connections."""
        self._close_file()
        self._close_fd()
        self._close_pyserial()

    def _close_file(self) -> None:
        """Close TextIOWrapper."""
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def _close_fd(self) -> None:
        """Close file descriptor."""
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None

    def _close_pyserial(self) -> None:
        """Close pyserial connection."""
        if self._ser:
            try:
                if self._ser.is_open:
                    self._ser.close()
            except Exception:
                pass
            self._ser = None


class SerialReader(QtCore.QThread):
    """Background thread that reads measurements from serial port.
    
    Signals:
        data_received: Emitted when a valid measurement is parsed.
        error: Emitted when an error occurs.
    """
    
    data_received = QtCore.Signal(object)
    error = QtCore.Signal(str)

    def __init__(self, port: str, baud: int = SerialConfig.DEFAULT_BAUD, parent=None):
        super().__init__(parent)
        self._port_handler = SerialPortHandler(port, baud)
        self._parser = MeasurementParser()
        self._running = False

    @property
    def port(self) -> str:
        """Get serial port path."""
        return self._port_handler.port

    def run(self) -> None:
        """Main thread loop: read and parse serial data."""
        try:
            self._port_handler.open()
        except ConnectionError as e:
            self.error.emit(str(e))
            return

        self._running = True
        
        while self._running:
            try:
                line = self._port_handler.readline()
                
                if not line:
                    continue

                measurement = self._parser.parse_line(line)
                
                if measurement:
                    self.data_received.emit(measurement.to_dict())
                    
            except (TypeError, OSError, IOError) as e:
                if not self._running:
                    break
                self.error.emit(f"Errore lettura seriale: {e}")
                break
            except Exception:
                if not self._running:
                    break
                self.error.emit(f"Errore inaspettato:\n{traceback.format_exc()}")
                break

        self._port_handler.close()

    def stop(self, wait_ms: int = 3000) -> None:
        """Stop the reader thread.
        
        Args:
            wait_ms: Maximum milliseconds to wait for thread to finish.
        """
        self._running = False
        self._port_handler.close()
        self.wait(wait_ms)
