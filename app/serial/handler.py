"""Low-level serial port I/O (chunked reads, line writes)."""

from __future__ import annotations

import logging
import os
import select
import sys
import time
from typing import Optional

import serial

from .config import SerialConfig

_IS_LINUX = sys.platform.startswith("linux")
if _IS_LINUX:
    import fcntl
    import termios

logger = logging.getLogger(__name__)


class DeviceDisconnected(ConnectionError):
    """The device went away (USB unplugged, port hung up)."""


# Machine-readable prefixes of ConnectionError messages raised by open(), so
# the UI can show an explanation instead of a raw errno string.
ERR_PORT_MISSING = "PORT_MISSING"
ERR_PORT_PERMISSION = "PORT_PERMISSION"
ERR_PORT_BUSY = "PORT_BUSY"


class SerialPortHandler:
    """Serial port wrapper.

    On Linux the port is opened as a raw file descriptor (termios) and polled
    with select(); this avoids pyserial's per-byte overhead and works with
    the ESP32-C3 USB-CDC at any nominal baud rate. Elsewhere pyserial is used.
    """

    def __init__(self, port: str, baud: int = SerialConfig.DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self._fd: Optional[int] = None
        self._ser: Optional[serial.Serial] = None

    @property
    def is_open(self) -> bool:
        return self._fd is not None or (self._ser is not None and self._ser.is_open)

    def open(self) -> None:
        if os.name == "posix" and not os.path.exists(self.port):
            raise ConnectionError(f"{ERR_PORT_MISSING}:{self.port}")
        try:
            self._open()
        except ConnectionError as e:
            cause = e.__cause__
            if isinstance(cause, PermissionError) or "Permission denied" in str(e):
                raise ConnectionError(f"{ERR_PORT_PERMISSION}:{self.port}") from e
            if "busy" in str(e).lower():
                raise ConnectionError(f"{ERR_PORT_BUSY}:{self.port}") from e
            raise

    def _open(self) -> None:
        if _IS_LINUX:
            try:
                self._open_direct()
                return
            except Exception as e1:
                logger.warning("Direct open of %s failed (%s), trying pyserial", self.port, e1)
                first_error = e1
        else:
            first_error = None
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.05, write_timeout=0.5)
            time.sleep(0.05)
            self._ser.reset_input_buffer()
        except Exception as e2:
            detail = f"{first_error}; {e2}" if first_error else str(e2)
            raise ConnectionError(detail) from e2

    def _open_direct(self) -> None:
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            speed = getattr(termios, f"B{self.baud}", termios.B115200)
            attrs[0] = 0                                                  # iflag
            attrs[1] = 0                                                  # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL       # cflag
            attrs[3] = 0                                                  # lflag
            attrs[4] = speed
            attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            # Keep the descriptor non-blocking: reads are gated by select().
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def read_chunk(self, timeout: float = 0.05, max_bytes: int = 65536) -> bytes:
        """Return whatever is available within `timeout` (may be b"")."""
        if self._fd is not None:
            ready, _, _ = select.select([self._fd], [], [], timeout)
            if not ready:
                return b""
            try:
                data = os.read(self._fd, max_bytes)
            except BlockingIOError:
                return b""
            except OSError as e:
                raise DeviceDisconnected(str(e)) from e
            if not data:
                # Readable but EOF: the tty was hung up (device unplugged).
                raise DeviceDisconnected("port hung up")
            return data
        if self._ser is not None:
            try:
                waiting = self._ser.in_waiting
                return self._ser.read(min(max(waiting, 1), max_bytes))
            except (serial.SerialException, OSError) as e:
                raise DeviceDisconnected(str(e)) from e
        return b""

    def write_line(self, text: str) -> None:
        data = (text.strip() + "\n").encode("ascii", errors="ignore")
        if self._fd is not None:
            view = memoryview(data)
            deadline = time.monotonic() + 0.5
            while view:
                try:
                    n = os.write(self._fd, view)
                    view = view[n:]
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("serial write timed out")
                    select.select([], [self._fd], [], 0.05)
                except OSError as e:
                    raise DeviceDisconnected(str(e)) from e
        elif self._ser is not None:
            try:
                self._ser.write(data)
            except (serial.SerialException, OSError) as e:
                raise DeviceDisconnected(str(e)) from e

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
