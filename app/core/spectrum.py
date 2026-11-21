"""Frequency spectrum analysis of load variations (DC systems).

Replaces the former AC-oriented harmonic analysis (THD, IEC limits), which
does not apply to the DC rails this instrument measures. What is useful on a
DC load is *where* the variation energy sits: periodic inference bursts,
switching-regulator ripple aliasing, thermal throttling cycles, etc.
"""

from __future__ import annotations

import math
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
    nyquist_hz: float = 0.0        # highest frequency represented (grid Nyquist)

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

    if len(t) <= max_points:
        grid = np.linspace(t[0], t[-1], len(t))
        y = np.interp(grid, t, y)
    else:
        # Capping the grid must not alias: low-pass the native-rate signal to
        # the decimated Nyquist before keeping every k-th point.
        decim = math.ceil(len(t) / max_points)
        native = np.linspace(t[0], t[-1], len(t))
        y = _anti_alias(np.interp(native, t, y), decim)
        grid = native[::decim]
        y = y[::decim]
    n = len(grid)
    dt = float(grid[1] - grid[0]) if n > 1 else 0.0
    if dt <= 0:
        return None

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
        nyquist_hz=0.5 / dt,
    )


def _anti_alias(y: np.ndarray, decim: int) -> np.ndarray:
    """Windowed-sinc low-pass at 80 % of the decimated Nyquist frequency.

    Applied by block FFT convolution (overlap-add), so neither the cost nor
    the memory grows with the decimation factor or with the recording length
    beyond the output itself. Zero-phase ('same' alignment), unity DC gain.
    """
    half = min(16 * decim, (len(y) - 1) // 2)
    if half < 1:
        return y
    m = np.arange(-half, half + 1)
    fc = 0.4 / decim                       # cycles per native sample
    h = 2.0 * fc * np.sinc(2.0 * fc * m) * np.hanning(2 * half + 1)
    h /= h.sum()
    nh = len(h)
    block = max(1 << 18, 8 * nh)
    nfft = 1 << (block + nh - 2).bit_length()
    hf = np.fft.rfft(h, nfft)
    out = np.zeros(len(y) + nh - 1)
    for start in range(0, len(y), block):
        seg = y[start:start + block]
        k = len(seg) + nh - 1
        out[start:start + k] += np.fft.irfft(np.fft.rfft(seg, nfft) * hf, nfft)[:k]
    return out[half:half + len(y)]
