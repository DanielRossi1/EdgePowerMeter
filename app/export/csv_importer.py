"""CSV import functionality for EdgePowerMeter."""

from __future__ import annotations
from pathlib import Path
from datetime import datetime
from typing import List, Optional
import csv

from ..core import MeasurementRecord


class CSVImporter:
    """Import measurement data from CSV files with auto-detection of format."""
    
    # Supported separators in order of priority
    SEPARATORS = [',', ';', '\t', ' ']
    
    # Supported timestamp formats
    TIMESTAMP_FORMATS = [
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
        '%Y/%m/%d %H:%M:%S.%f',
        '%Y/%m/%d %H:%M:%S',
        '%d-%m-%Y %H:%M:%S.%f',
        '%d-%m-%Y %H:%M:%S',
        '%d/%m/%Y %H:%M:%S.%f',
        '%d/%m/%Y %H:%M:%S',
    ]
    
    @classmethod
    def detect_separator(cls, filepath: Path) -> str:
        """Detect the separator used in a CSV file.
        
        Args:
            filepath: Path to the CSV file
            
        Returns:
            Detected separator character
            
        Raises:
            ValueError: If no valid separator is detected
        """
        with open(filepath, 'r', encoding='utf-8') as f:
            # Read first few lines for detection
            lines = [f.readline() for _ in range(5)]
        
        # Filter out empty lines
        lines = [line.strip() for line in lines if line.strip()]
        if not lines:
            raise ValueError("File is empty")
        
        # Count separators in each line and find most consistent one
        best_separator = None
        best_score = 0
        
        for sep in cls.SEPARATORS:
            counts = [line.count(sep) for line in lines]
            # Good separator: consistent count >= 3 (need at least 4 columns)
            if counts and min(counts) >= 3 and max(counts) == min(counts):
                score = min(counts)
                if score > best_score:
                    best_score = score
                    best_separator = sep
        
        if best_separator is None:
            raise ValueError(
                "Could not detect CSV separator. "
                "Supported separators: comma, semicolon, tab, space"
            )
        
        return best_separator
    
    @classmethod
    def parse_timestamp(cls, ts_str: str) -> datetime:
        """Parse timestamp string trying multiple formats.
        
        Args:
            ts_str: Timestamp string
            
        Returns:
            Parsed datetime
            
        Raises:
            ValueError: If timestamp format is not recognized
        """
        ts_str = ts_str.strip()
        
        for fmt in cls.TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(ts_str, fmt)
            except ValueError:
                continue
        
        raise ValueError(f"Unrecognized timestamp format: {ts_str}")
    
    @classmethod
    def import_csv(cls, filepath: Path) -> List[MeasurementRecord]:
        """Import measurements from a CSV file.
        
        Auto-detects separator and timestamp format. Expects columns:
        Timestamp, Voltage, Current, Power (header names are flexible).
        
        Args:
            filepath: Path to the CSV file
            
        Returns:
            List of MeasurementRecord objects
            
        Raises:
            ValueError: If file format is invalid
            FileNotFoundError: If file doesn't exist
        """
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {filepath}")
        
        separator = cls.detect_separator(filepath)
        records: List[MeasurementRecord] = []
        
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=separator)
            
            # Read header
            header = next(reader, None)
            if not header or len(header) < 4:
                raise ValueError(
                    "Invalid CSV format. Expected at least 4 columns: "
                    "Timestamp, Voltage, Current, Power"
                )
            
            # Detect if file has RelativeTime column (5 columns).
            # The RelativeTime column carries microsecond-accurate timing that the
            # millisecond-truncated timestamp cannot, so we preserve it when present.
            has_relative_time_col = len(header) >= 5 and 'relative' in header[1].lower()

            # Parse data rows
            line_num = 1
            start_unix: Optional[float] = None

            for row in reader:
                line_num += 1

                if len(row) < 4:
                    continue  # Skip incomplete rows

                try:
                    # Clean values (remove units if present)
                    ts_str = row[0].strip()
                    relative_time: Optional[float] = None

                    # Handle both old format (4 cols) and new format (5 cols with RelativeTime)
                    if has_relative_time_col and len(row) >= 5:
                        # New format: Timestamp, RelativeTime, Voltage, Current, Power
                        relative_time = float(row[1].strip().replace(',', '.'))
                        voltage = float(row[2].strip().replace(',', '.'))
                        current = float(row[3].strip().replace(',', '.'))
                        power = float(row[4].strip().replace(',', '.'))
                    else:
                        # Old format: Timestamp, Voltage, Current, Power
                        voltage = float(row[1].strip().replace(',', '.'))
                        current = float(row[2].strip().replace(',', '.'))
                        power = float(row[3].strip().replace(',', '.'))

                    timestamp = cls.parse_timestamp(ts_str)
                    unix_time = timestamp.timestamp()

                    # Track start time for relative time calculation
                    if start_unix is None:
                        start_unix = unix_time

                    records.append(MeasurementRecord(
                        timestamp=timestamp,
                        unix_time=unix_time,
                        # Fall back to timestamp-derived time only when column is absent
                        relative_time=relative_time if relative_time is not None
                        else unix_time - start_unix,
                        voltage=voltage,
                        current=current,
                        power=power
                    ))
                except (ValueError, IndexError):
                    # Skip invalid rows but continue processing
                    continue

        if not records:
            raise ValueError("No valid data rows found in CSV file")

        if has_relative_time_col:
            # Trust the precise RelativeTime column: sort by it and rebase to 0.
            records.sort(key=lambda r: r.relative_time)
            offset = records[0].relative_time
            if offset != 0.0:
                for r in records:
                    object.__setattr__(r, 'relative_time', r.relative_time - offset)
        else:
            # Reconstruct relative time from (lower-precision) absolute timestamps.
            records.sort(key=lambda r: r.unix_time)
            start_unix = records[0].unix_time
            for r in records:
                # Use object.__setattr__ since dataclass might be frozen
                object.__setattr__(r, 'relative_time', r.unix_time - start_unix)

        return records
