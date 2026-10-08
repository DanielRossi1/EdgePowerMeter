"""Energy-efficiency figures for a workload (e.g. AI inference) over a time range.

Inputs: either the number of work units completed in the range (inferences,
frames, ...) or the throughput in units per second, and optionally the idle
power of the device. Outputs: energy per unit, units per joule and
throughput per watt (FPS/W), both gross and net of the idle power (the net
values isolate the energy actually spent by the workload).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .statistics import Statistics


@dataclass
class BenchmarkInput:
    units: Optional[float] = None          # work units completed in the range
    rate: Optional[float] = None           # or throughput, units per second
    idle_power_w: Optional[float] = None   # baseline to subtract (net figures)


@dataclass
class BenchmarkResult:
    duration_s: float
    avg_power_w: float
    energy_j: float
    units: float
    rate: float                    # units / s
    energy_per_unit_j: float
    units_per_joule: float
    rate_per_watt: float           # e.g. FPS/W
    idle_power_w: Optional[float] = None
    net_power_w: Optional[float] = None
    net_energy_per_unit_j: Optional[float] = None
    net_rate_per_watt: Optional[float] = None


def compute(stats: Optional[Statistics], inp: BenchmarkInput) -> Optional[BenchmarkResult]:
    """None when the range or the inputs are not usable."""
    if stats is None or stats.duration_seconds <= 0:
        return None
    t = stats.duration_seconds
    energy = stats.energy_wh * 3600.0
    # Average power from the integrated energy, consistent with the energy
    # figures even with uneven sample spacing or gaps.
    power = energy / t
    if inp.units is not None and inp.units > 0:
        units = float(inp.units)
        rate = units / t
    elif inp.rate is not None and inp.rate > 0:
        rate = float(inp.rate)
        units = rate * t
    else:
        return None
    if power <= 0:
        return None
    res = BenchmarkResult(
        duration_s=t,
        avg_power_w=power,
        energy_j=energy,
        units=units,
        rate=rate,
        energy_per_unit_j=energy / units,
        units_per_joule=units / energy if energy > 0 else 0.0,
        rate_per_watt=rate / power,
    )
    if inp.idle_power_w is not None and inp.idle_power_w >= 0:
        net = power - inp.idle_power_w
        res.idle_power_w = inp.idle_power_w
        res.net_power_w = net
        if net > 0:
            res.net_energy_per_unit_j = net * t / units
            res.net_rate_per_watt = rate / net
    return res
