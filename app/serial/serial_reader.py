"""Serial port reader thread for EdgePowerMeter.

This module provides a thread-safe serial reader that parses measurement data
from the EdgePowerMeter firmware and emits it as Qt signals.
"""

from __future__ import annotations

import traceback

from PySide6 import QtCore

from .config import SerialConfig
from .handler import SerialPortHandler
from .parser import MeasurementParser
from .sampler import SampleRateController


class SerialReader(QtCore.QThread):
    """Background thread that reads measurements from serial port.
    
    Signals:
        data_received: Emitted when a valid measurement is parsed.
        error: Emitted when an error occurs.
    """
    
    data_received = QtCore.Signal(object)
    error = QtCore.Signal(str)

    def __init__(self, port: str, baud: int = SerialConfig.DEFAULT_BAUD, 
                 target_sample_rate: int = 0, max_device_rate: int = 400, parent=None):
        super().__init__(parent)
        self._port_handler = SerialPortHandler(port, baud)
        self._parser = MeasurementParser()
        self._sampler = SampleRateController(target_sample_rate, max_device_rate=max_device_rate)
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

                # Check if we should accept this sample (subsampling control)
                if self._sampler.should_accept_sample():
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
