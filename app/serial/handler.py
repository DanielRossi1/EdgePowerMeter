"""Low-level serial port handler."""

from __future__ import annotations

import io
import logging
import os
import sys
import time
from typing import Optional

import serial

from .config import SerialConfig

# Platform-specific imports for direct serial access (Linux only)
_IS_LINUX = sys.platform.startswith('linux')
if _IS_LINUX:
    import fcntl
    import termios

logger = logging.getLogger(__name__)


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
        """Open serial port, trying direct method first (Linux), then pyserial."""
        # On Windows, only use pyserial
        if not _IS_LINUX:
            try:
                self._open_pyserial()
                self._use_direct = False
                logger.info(f"Opened {self.port} using pyserial")
            except Exception as e:
                raise ConnectionError(
                    f"Cannot open {self.port}: {e}\n"
                    "Check that the device exists and permissions are correct."
                ) from e
            return
        
        # On Linux, try direct method first, then pyserial
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
