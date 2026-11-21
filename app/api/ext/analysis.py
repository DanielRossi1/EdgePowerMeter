"""Handlers of the `analysis` extension: spectrum, power-supply quality, distribution and
event (peak) detection over a range of the recording.

Each handler copies its range through `service.range_samples` (the copy is taken on the GUI
thread, the rest of the work runs on the HTTP worker thread) and returns a JSON-able dict.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from app.api.schema import RANGE_PROPERTIES
from app.api.service import ApiError
from app.core.power_supply_quality import PowerSupplyAnalyzer
from app.core.samples import Samples
from app.core.spectrum import MIN_SAMPLES as SPECTRUM_MIN_SAMPLES
from app.core.spectrum import analyze_spectrum

from .analysis_schema import PERCENTILES_DEFAULT, PERCENTILES_MAX_ITEMS

_RANGE_KEYS = tuple(RANGE_PROPERTIES)
_QUANTITIES = ("voltage", "current", "power")


def _fetch(service, kwargs: Dict[str, Any]) -> Tuple[Samples, Dict[str, Any]]:
    """Range copy and its description. Only the range keys are passed on."""
    params = {k: kwargs[k] for k in _RANGE_KEYS if k in kwargs}
    t0, t1, label, part = service.range_samples(params)
    return part, {"t0": t0, "t1": t1, "label": label, "samples": len(part)}


def _finite(values: np.ndarray, what: str) -> np.ndarray:
    if not np.all(np.isfinite(values)):
        raise ApiError(f"the {what} of the range contains NaN or infinite values")
    return values


def _pkey(p: float) -> str:
    return "p{:g}".format(float(p))


def _percentile_list(percentiles: Optional[List[float]]) -> List[float]:
    pcts = list(PERCENTILES_DEFAULT if percentiles is None else percentiles)
    if not pcts:
        raise ApiError("percentiles needs at least 1 item")
    if len(pcts) > PERCENTILES_MAX_ITEMS:
        raise ApiError(f"percentiles must have at most {PERCENTILES_MAX_ITEMS} items "
                       f"(got {len(pcts)})")
    for p in pcts:
        if not 0 <= float(p) <= 100:
            raise ApiError(f"percentiles must be between 0 and 100 (got {p:g})")
    # The keys use 6 significant digits ('p99.9'): two percentiles that print the same would
    # overwrite each other in the result, so they are refused instead.
    seen: Dict[str, float] = {}
    for p in pcts:
        key = _pkey(p)
        if key in seen:
            raise ApiError(f"percentiles too close: {seen[key]:g} and {float(p):g} would both "
                           f"be reported as '{key}': use values that differ more")
        seen[key] = float(p)
    return pcts


def _spectrum_points(freqs: np.ndarray, amps: np.ndarray, points_out: int
                     ) -> Tuple[List[float], List[float]]:
    """Max-pool the amplitude spectrum into at most points_out bins.

    Each bin keeps its largest amplitude and the frequency of that bin's maximum, so a
    narrow peak survives the reduction.
    """
    k = min(points_out, len(amps))
    edges = np.linspace(0, len(amps), k + 1).astype(np.int64)  # k non-empty bins
    out_f: List[float] = []
    out_a: List[float] = []
    for a, b in zip(edges[:-1], edges[1:]):
        j = int(a) + int(np.argmax(amps[a:b]))
        out_f.append(float(freqs[j]))
        out_a.append(float(amps[j]))
    return out_f, out_a


def _event_energy(t: np.ndarray, x: np.ndarray) -> float:
    """Trapezoid integral of x over t (joules when x is watts and t is seconds)."""
    if len(x) < 2:
        return 0.0
    return float(np.sum((x[1:] + x[:-1]) * 0.5 * np.diff(t)))


def handlers(service) -> Dict[str, Callable[..., dict]]:
    """Tool name -> handler, as `app.api.ext` expects. Each handler gets validated kwargs."""

    def analyze_spectrum_tool(signal: str = "current", n_peaks: int = 5,
                              max_points: int = 1 << 20, points_out: int = 256,
                              **range_args: Any) -> dict:
        part, rng = _fetch(service, range_args)
        base = {"range": rng, "signal": signal}
        n = len(part)
        if n < SPECTRUM_MIN_SAMPLES:
            return dict(base, available=False, reason=f"need at least {SPECTRUM_MIN_SAMPLES} "
                        f"samples in the range (got {n})")
        y = _finite(part.column(signal).astype(np.float64), signal)
        if float(y.max() - y.min()) == 0.0:
            return dict(base, available=False,
                        reason=f"no variation in the {signal} over the range (constant value)")
        if part.duration <= 0:
            return dict(base, available=False, reason="the range has zero duration")
        res = analyze_spectrum(part, signal=signal, max_points=max_points, n_peaks=n_peaks)
        if res is None:
            return dict(base, available=False, reason="the range is too short or has no variation")
        freqs, amps = _spectrum_points(res.frequencies, res.amplitudes, points_out)
        decimated = n > max_points
        # The anti-alias filter passes components up to 0.8 x the decimated Nyquist frequency.
        note = (f"components above {0.8 * res.nyquist_hz:.4g} Hz were filtered out before the FFT"
                if decimated else None)
        return dict(
            base,
            available=True,
            sample_rate=res.sample_rate,
            nyquist_hz=res.nyquist_hz,
            resolution_hz=res.resolution_hz,
            decimated=decimated,
            effective_sample_rate_hz=res.sample_rate,
            note=note,
            mean=res.mean,
            std=res.std,
            modulation_percent=res.modulation_percent,
            peaks=[{"frequency_hz": p.frequency, "amplitude": p.amplitude} for p in res.peaks],
            spectrum={"frequency_hz": freqs, "amplitude": amps},
        )

    def power_quality_tool(nominal_voltage: Optional[float] = None,
                           **range_args: Any) -> dict:
        part, rng = _fetch(service, range_args)
        if len(part) < PowerSupplyAnalyzer.MIN_SAMPLES:
            raise ApiError(f"need at least {PowerSupplyAnalyzer.MIN_SAMPLES} samples in the "
                           f"range (got {len(part)})")
        q = PowerSupplyAnalyzer().analyze_voltage_quality(part, nominal_voltage)
        if q is None:  # not reached with the sample check above; kept as a guard
            raise ApiError("not enough samples in the range for a power quality analysis")
        out = dataclasses.asdict(q)
        out["nominal_voltage_source"] = "given" if nominal_voltage is not None else "mean"
        return dict(out, range=rng)

    def distribution_tool(quantity: str = "power", percentiles: Optional[List[float]] = None,
                          bins: int = 40, **range_args: Any) -> dict:
        pcts = _percentile_list(percentiles)
        part, rng = _fetch(service, range_args)
        x = _finite(part.column(quantity).astype(np.float64), quantity)
        counts, edges = np.histogram(x, bins=bins)
        values = np.percentile(x, pcts)
        return {
            "range": rng,
            "quantity": quantity,
            "count": int(x.size),
            "mean": float(x.mean()),
            "std": float(x.std()),
            "min": float(x.min()),
            "max": float(x.max()),
            "percentiles": {_pkey(p): float(v) for p, v in zip(pcts, values)},
            "histogram": {"edges": [float(e) for e in edges],
                          "counts": [int(c) for c in counts]},
        }

    def detect_peaks_tool(quantity: str = "power", threshold: Optional[float] = None,
                          threshold_sigma: float = 3.0, min_duration_s: float = 0.0,
                          max_events: int = 100, **range_args: Any) -> dict:
        part, rng = _fetch(service, range_args)
        x = _finite(part.column(quantity).astype(np.float64), quantity)
        t = part.t.astype(np.float64)
        base_mean = float(x.mean())
        base_std = float(x.std())
        if threshold is not None:
            thr = float(threshold)
        else:
            thr = base_mean + float(threshold_sigma) * base_std

        above = (x > thr).astype(np.int64)
        edge = np.diff(np.concatenate(([0], above, [0])))
        starts = np.flatnonzero(edge == 1)      # first sample of each run
        ends = np.flatnonzero(edge == -1)       # one past the last sample of each run
        durations = t[ends - 1] - t[starts]
        keep = durations >= float(min_duration_s)
        starts, ends, durations = starts[keep], ends[keep], durations[keep]

        count = int(starts.size)
        span = float(t[-1] - t[0]) if t.size > 1 else 0.0
        events: List[dict] = []
        # Per-event statistics only for the listed events: the count covers all of them.
        for a, b in zip(starts[:max_events], ends[:max_events]):
            a, b = int(a), int(b)
            seg = x[a:b]
            ev = {
                "t_start": float(t[a]),
                "t_end": float(t[b - 1]),
                "duration_s": float(t[b - 1] - t[a]),
                "peak": float(seg.max()),
                "mean": float(seg.mean()),
            }
            if quantity == "power":
                ev["energy_j"] = _event_energy(t[a:b], seg)
            events.append(ev)

        return {
            "range": rng,
            "quantity": quantity,
            "threshold_used": thr,
            "baseline_mean": base_mean,
            "baseline_std": base_std,
            "count": count,
            "truncated": count > max_events,
            "rate_per_s": (count / span) if span > 0 else None,
            "mean_duration_s": float(durations.mean()) if count else None,
            "events": events,
        }

    return {
        "analyze_spectrum": analyze_spectrum_tool,
        "power_quality": power_quality_tool,
        "distribution": distribution_tool,
        "detect_peaks": detect_peaks_tool,
    }
