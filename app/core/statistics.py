"""Statistical analysis of measurements (vectorized)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .samples import Samples


@dataclass
class Statistics:
    """Statistical summary of a run of samples."""

    count: int
    duration_seconds: float
    sample_rate_hz: float
    voltage_min: float
    voltage_max: float
    voltage_avg: float
    voltage_std: float
    current_min: float
    current_max: float
    current_avg: float
    current_std: float
    current_rms: float
    power_min: float
    power_max: float
    power_avg: float
    power_std: float
    energy_wh: float
    charge_ah: float

    @classmethod
    def from_samples(cls, s: Samples) -> Optional["Statistics"]:
        """Compute statistics; None when fewer than 2 samples."""
        n = len(s)
        if n < 2:
            return None

        v = s.v.astype(np.float64)
        i = s.i.astype(np.float64)
        p = s.p.astype(np.float64)
        dt = np.diff(s.t)

        duration = float(s.t[-1] - s.t[0])
        if duration <= 0:
            # Degenerate time base (e.g. imported file with identical
            # timestamps): there is no way to integrate over time, so energy
            # and charge are reported as 0 instead of being invented from an
            # assumed sample rate.
            duration = 0.0
            energy_ws = charge_as = 0.0
        else:
            energy_ws, charge_as = integrate_energy(dt, p, i, gap_threshold(dt))

        return cls(
            count=n,
            duration_seconds=duration,
            sample_rate_hz=(n - 1) / duration if duration > 0 else 0.0,
            voltage_min=float(v.min()),
            voltage_max=float(v.max()),
            voltage_avg=float(v.mean()),
            voltage_std=float(v.std(ddof=1)),
            current_min=float(i.min()),
            current_max=float(i.max()),
            current_avg=float(i.mean()),
            current_std=float(i.std(ddof=1)),
            current_rms=float(np.sqrt(np.mean(i * i))),
            power_min=float(p.min()),
            power_max=float(p.max()),
            power_avg=float(p.mean()),
            power_std=float(p.std(ddof=1)),
            energy_wh=energy_ws / 3600.0,
            charge_ah=charge_as / 3600.0,
        )


def gap_threshold(dt: np.ndarray) -> float:
    """Interval length above which two samples are considered disconnected
    (device unplugged and reconnected, stream paused): 10x the typical sample
    interval, but never below 2 s so slow sample rates are not affected."""
    typical = float(np.median(dt)) if len(dt) else 0.0
    return max(2.0, 10.0 * typical)


def integrate_energy(dt: np.ndarray, p: np.ndarray, i: np.ndarray,
                     max_gap: Optional[float] = None) -> tuple[float, float]:
    """Trapezoidal energy [Ws] and charge [As] integration.

    Negative power/current (sensor offset at zero load) is clamped to 0 so
    that noise around zero does not subtract energy. Intervals longer than
    `max_gap` (e.g. a reconnect gap) are skipped.
    """
    p_mid = np.maximum(0.0, (p[1:] + p[:-1]) * 0.5)
    i_mid = np.maximum(0.0, (i[1:] + i[:-1]) * 0.5)
    if max_gap is not None:
        dt = np.where(dt > max_gap, 0.0, dt)
    dt = np.maximum(dt, 0.0)
    return float(np.dot(p_mid, dt)), float(np.dot(i_mid, dt))


class RunningStats:
    """Incrementally updated statistics for live display (O(batch) per update)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.count = 0
        self.power_sum = 0.0
        self.energy_ws = 0.0
        self.charge_as = 0.0
        self.v_min = self.i_min = self.p_min = float("inf")
        self.v_max = self.i_max = self.p_max = float("-inf")
        self._last: Optional[tuple[float, float, float]] = None  # t, i, p
        self._gap: Optional[float] = None

    def update(self, b: Samples) -> None:
        n = len(b)
        if not n:
            return
        p = b.p.astype(np.float64)
        i = b.i.astype(np.float64)
        t = b.t
        if self._last is not None:
            lt, li, lp = self._last
            t = np.concatenate(([lt], t))
            p_all = np.concatenate(([lp], p))
            i_all = np.concatenate(([li], i))
        else:
            p_all, i_all = p, i
        if len(t) >= 2:
            dt = np.diff(t)
            if len(dt) >= 8 or self._gap is None:
                self._gap = gap_threshold(dt)
            e, q = integrate_energy(dt, p_all, i_all, self._gap)
            self.energy_ws += e
            self.charge_as += q
        self._last = (float(b.t[-1]), float(i[-1]), float(p[-1]))

        self.count += n
        self.power_sum += float(p.sum())
        self.v_min = min(self.v_min, float(b.v.min()))
        self.v_max = max(self.v_max, float(b.v.max()))
        self.i_min = min(self.i_min, float(i.min()))
        self.i_max = max(self.i_max, float(i.max()))
        self.p_min = min(self.p_min, float(p.min()))
        self.p_max = max(self.p_max, float(p.max()))

    @property
    def power_avg(self) -> float:
        return self.power_sum / self.count if self.count else 0.0

    def rebuild(self, s: Samples) -> None:
        self.reset()
        self.update(s)
