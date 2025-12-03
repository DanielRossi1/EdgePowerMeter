"""Measurement data parser for serial communication."""

from __future__ import annotations
from datetime import datetime
from typing import Optional

from ..core import Measurement
from .config import SerialConfig


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
