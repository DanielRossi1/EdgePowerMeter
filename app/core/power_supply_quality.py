"""Power supply quality analysis for DC systems.

Metrics to evaluate a DC supply feeding the device under test:
- voltage regulation, peak-to-peak ripple and RMS noise
- load regulation and settling time after the first significant load step
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from ..i18n import tr
from .samples import Samples

# Rating identifiers (stable, untranslated); use rating_label() for display.
RATING_EXCELLENT = "excellent"
RATING_GOOD = "good"
RATING_FAIR = "fair"
RATING_POOR = "poor"


def rating_label(rating: str) -> str:
    return {
        RATING_EXCELLENT: tr("Excellent"),
        RATING_GOOD: tr("Good"),
        RATING_FAIR: tr("Fair"),
        RATING_POOR: tr("Poor"),
    }.get(rating, rating)


@dataclass
class PowerSupplyQuality:
    """Power supply quality metrics for DC systems."""

    nominal_voltage: float
    min_voltage: float
    max_voltage: float
    voltage_ripple_percent: float
    voltage_ripple_mv: float
    load_regulation_percent: Optional[float] = None
    settling_time_ms: Optional[float] = None
    rms_noise: float = 0.0
    std_deviation: float = 0.0
    stability_rating: str = RATING_POOR
    meets_1percent_spec: bool = False
    meets_01percent_spec: bool = False
    meets_005percent_spec: bool = False


class PowerSupplyAnalyzer:
    """Analyzer for DC power supply quality."""

    EXCELLENT_THRESHOLD = 0.05  # % ripple
    GOOD_THRESHOLD = 0.1
    FAIR_THRESHOLD = 1.0

    MIN_SAMPLES = 10
    SETTLING_WINDOW_S = 0.5     # how long after a step we look for settling

    def analyze_voltage_quality(self, s: Samples,
                                nominal_voltage: Optional[float] = None
                                ) -> Optional[PowerSupplyQuality]:
        if len(s) < self.MIN_SAMPLES:
            return None

        v = s.v.astype(np.float64)
        v_min, v_max = float(v.min()), float(v.max())
        v_mean = float(v.mean())
        v_std = float(v.std())
        nominal = v_mean if nominal_voltage is None else float(nominal_voltage)

        ripple_v = v_max - v_min
        # A ~0 V rail (no source connected) has no meaningful relative ripple.
        ripple_pct = 0.0 if abs(nominal) < 1e-9 else ripple_v / abs(nominal) * 100.0

        if ripple_pct < self.EXCELLENT_THRESHOLD:
            rating = RATING_EXCELLENT
        elif ripple_pct < self.GOOD_THRESHOLD:
            rating = RATING_GOOD
        elif ripple_pct < self.FAIR_THRESHOLD:
            rating = RATING_FAIR
        else:
            rating = RATING_POOR

        load_reg, settling = self._analyze_load_step(s, v, nominal)

        return PowerSupplyQuality(
            nominal_voltage=nominal,
            min_voltage=v_min,
            max_voltage=v_max,
            voltage_ripple_percent=ripple_pct,
            voltage_ripple_mv=ripple_v * 1000.0,
            load_regulation_percent=load_reg,
            settling_time_ms=settling,
            rms_noise=float(np.sqrt(np.mean((v - v_mean) ** 2))),
            std_deviation=v_std,
            stability_rating=rating,
            meets_1percent_spec=ripple_pct < 1.0,
            meets_01percent_spec=ripple_pct < 0.1,
            meets_005percent_spec=ripple_pct < 0.05,
        )

    def _analyze_load_step(self, s: Samples, v: np.ndarray, nominal: float
                           ) -> Tuple[Optional[float], Optional[float]]:
        """Load regulation and settling time around the first load step.

        A load step is a current change larger than 4 sigma of the sample to
        sample current noise *and* larger than 10 % of the current range, so
        that plain sensor noise on a constant load is not reported as a step.
        Settling is measured in time (not samples), so the result does not
        depend on the sample rate.
        """
        i = s.i.astype(np.float64)
        t = s.t
        if len(i) < 20:
            return None, None

        di = np.diff(i)
        noise = float(np.median(np.abs(di - np.median(di)))) * 1.4826  # robust sigma
        span = float(i.max() - i.min())
        threshold = max(4.0 * noise, 0.1 * span)
        if span <= 0 or threshold <= 0:
            return None, None
        steps = np.flatnonzero(np.abs(di) > threshold)
        if not len(steps):
            return None, None
        k = int(steps[0]) + 1  # first sample after the step
        if k < 5:
            return None, None

        t_step = t[k]
        pre = v[max(0, k - 50):k]
        v_before = float(pre.mean())
        end = int(np.searchsorted(t, t_step + self.SETTLING_WINDOW_S))
        post = v[k:end]
        if len(post) < 10:
            return None, None
        tail = post[-max(5, len(post) // 5):]
        v_after = float(tail.mean())
        band = max(3.0 * float(tail.std()), 0.001 * abs(nominal), 1.25e-3)

        outside = np.flatnonzero(np.abs(post - v_after) > band)
        settle_idx = k + (int(outside[-1]) + 1 if len(outside) else 0)
        settle_idx = min(settle_idx, len(t) - 1)

        load_reg = 0.0 if abs(nominal) < 1e-9 else abs(v_after - v_before) / abs(nominal) * 100.0
        return load_reg, float(t[settle_idx] - t_step) * 1000.0

    @staticmethod
    def get_quality_recommendations(q: PowerSupplyQuality) -> List[str]:
        rec: List[str] = []
        if q.stability_rating == RATING_EXCELLENT:
            rec.append(tr("Excellent voltage stability, suitable for precision applications."))
        elif q.stability_rating == RATING_GOOD:
            rec.append(tr("Good voltage stability, suitable for most applications."))
        elif q.stability_rating == RATING_FAIR:
            rec.append(tr("Fair voltage stability: consider a better supply for sensitive loads."))
            rec.append(tr("Add output filtering capacitors to reduce ripple."))
        else:
            rec.append(tr("Poor voltage stability: not recommended for sensitive electronics."))
            rec.append(tr("Check cables and connections, or add an LC filter on the output."))

        if q.load_regulation_percent is not None:
            if q.load_regulation_percent < 1.0:
                rec.append(tr("Good load regulation."))
            elif q.load_regulation_percent < 3.0:
                rec.append(tr("Fair load regulation: the voltage drops under load."))
            else:
                rec.append(tr("Poor load regulation: significant voltage drop under load."))

        if q.settling_time_ms is not None:
            if q.settling_time_ms < 10:
                rec.append(tr("Fast transient response (under 10 ms)."))
            elif q.settling_time_ms < 100:
                rec.append(tr("Good transient response (under 100 ms)."))
            else:
                rec.append(tr("Slow transient response: may affect dynamic loads."))
        return rec
