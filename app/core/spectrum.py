"""Frequency spectrum analysis of load variations (DC systems).

Replaces the former AC-oriented harmonic analysis (THD, IEC limits), which
does not apply to the DC rails this instrument measures. What is useful on a
DC load is *where* the variation energy sits: periodic inference bursts,
switching-regulator ripple aliasing, thermal throttling cycles, etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .samples import Samples

MIN_SAMPLES = 64


@dataclass
class SpectrumPeak:
    frequency: float
    amplitude: float


@dataclass
class SpectrumResult:
    signal: str                    # 'voltage' | 'current' | 'power'
    sample_rate: float             # Hz (uniform grid used for the FFT)
    mean: float                    # DC level
    std: float                     # AC (variation) RMS
    modulation_percent: float      # std / mean * 100
    frequencies: np.ndarray
    amplitudes: np.ndarray         # single-sided amplitude spectrum
    peaks: List[SpectrumPeak] = field(default_factory=list)

    @property
    def dominant(self) -> Optional[SpectrumPeak]:
        return self.peaks[0] if self.peaks else None

    @property
    def resolution_hz(self) -> float:
        return float(self.frequencies[1] - self.frequencies[0]) if len(self.frequencies) > 1 else 0.0


def analyze_spectrum(s: Samples, signal: str = "current",
                     max_points: int = 1 << 20, n_peaks: int = 5) -> Optional[SpectrumResult]:
    """FFT of the selected signal.

    Samples are resampled onto a uniform time grid first: the FFT assumes
    uniform spacing, and real recordings have jitter and occasional gaps.
    Returns None when there is too little data or no variation at all.
    """
    if len(s) < MIN_SAMPLES or s.duration <= 0:
        return None
    y = s.column(signal).astype(np.float64)
    t = s.t

    n = min(len(t), max_points)
    grid = np.linspace(t[0], t[-1], n)
    dt = float(grid[1] - grid[0])
    if dt <= 0:
        return None
    y = np.interp(grid, t, y)

    mean = float(y.mean())
    ac = y - mean
    std = float(ac.std())
    if std <= 0:
        return None

    window = np.hanning(n)
    # Divide by the window's coherent gain so peak amplitudes are in signal units.
    spec = np.abs(np.fft.rfft(ac * window)) * 2.0 / window.sum()
    freqs = np.fft.rfftfreq(n, dt)

    peaks: List[SpectrumPeak] = []
    if len(spec) > 3:
        interior = spec[1:-1]
        is_peak = (interior > spec[:-2]) & (interior >= spec[2:])
        idx = np.flatnonzero(is_peak) + 1
        idx = idx[freqs[idx] > 0]
        top = idx[np.argsort(spec[idx])[::-1][:n_peaks]]
        peaks = [SpectrumPeak(float(freqs[k]), float(spec[k])) for k in top]

    return SpectrumResult(
        signal=signal,
        sample_rate=1.0 / dt,
        mean=mean,
        std=std,
        modulation_percent=abs(std / mean) * 100.0 if abs(mean) > 1e-12 else 0.0,
        frequencies=freqs,
        amplitudes=spec,
        peaks=peaks,
    )
