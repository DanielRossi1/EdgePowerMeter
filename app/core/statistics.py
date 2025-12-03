"""Statistical analysis of measurements."""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, TYPE_CHECKING
import statistics as stats_module

if TYPE_CHECKING:
    from .measurement import MeasurementRecord


@dataclass
class Statistics:
    """Statistical summary of measurements."""
    count: int
    duration_seconds: float
    voltage_min: float
    voltage_max: float
    voltage_avg: float
    voltage_std: float
    current_min: float
    current_max: float
    current_avg: float
    current_std: float
    power_min: float
    power_max: float
    power_avg: float
    power_std: float
    energy_wh: float
    charge_ah: float
    
    @classmethod
    def from_records(cls, records: List['MeasurementRecord']) -> 'Statistics | None':
        """Calculate statistics from measurement records."""
        if len(records) < 2:
            return None
        
        voltages = [r.voltage for r in records]
        currents = [r.current for r in records]
        powers = [r.power for r in records]
        
        duration = records[-1].unix_time - records[0].unix_time
        if duration <= 0:
            duration = len(records) * 0.1
        
        # Calculate energy and charge using trapezoidal integration
        energy_ws = 0.0
        charge_as = 0.0
        for i in range(1, len(records)):
            dt = records[i].unix_time - records[i-1].unix_time
            avg_power = (records[i].power + records[i-1].power) / 2
            avg_current = (records[i].current + records[i-1].current) / 2
            energy_ws += avg_power * dt
            charge_as += avg_current * dt
        
        return cls(
            count=len(records),
            duration_seconds=duration,
            voltage_min=min(voltages),
            voltage_max=max(voltages),
            voltage_avg=stats_module.mean(voltages),
            voltage_std=stats_module.stdev(voltages) if len(voltages) > 1 else 0,
            current_min=min(currents),
            current_max=max(currents),
            current_avg=stats_module.mean(currents),
            current_std=stats_module.stdev(currents) if len(currents) > 1 else 0,
            power_min=min(powers),
            power_max=max(powers),
            power_avg=stats_module.mean(powers),
            power_std=stats_module.stdev(powers) if len(powers) > 1 else 0,
            energy_wh=energy_ws / 3600,
            charge_ah=charge_as / 3600,
        )
