"""Value formatting helpers shared by the UI and the reports."""

from __future__ import annotations

import math

from ..i18n import tr

_PREFIXES = ((1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"))


def format_si(value: float, unit: str, digits: int = 4) -> str:
    """Format with an engineering prefix: 0.01234 A -> '12.34 mA'.

    `digits` is the number of significant digits.
    """
    if value is None or not math.isfinite(value):
        return "-"
    mag = abs(value)
    if mag == 0:
        return f"0 {unit}"
    for factor, prefix in _PREFIXES:
        if mag >= factor * 0.9995:
            break
    scaled = value / factor
    decimals = max(0, digits - 1 - int(math.floor(math.log10(abs(scaled)))))
    return f"{scaled:.{decimals}f} {prefix}{unit}"


def scale_for_mode(value: float, unit: str, mode: str, decimals: int) -> tuple[str, str]:
    """Return (number, unit) according to the unit display mode.

    mode: 'auto' (engineering prefix), 'base' (V/A/W), 'milli' (mV/mA/mW).
    """
    if value is None or not math.isfinite(value):
        return "-", unit
    if mode == "base":
        return f"{value:.{decimals}f}", unit
    if mode == "milli":
        return f"{value * 1e3:.{max(0, decimals - 3)}f}", "m" + unit
    mag = abs(value)
    for factor, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ")):
        if mag >= factor * 0.9995 or factor == 1e-6:
            break
    if mag == 0:
        factor, prefix = 1.0, ""
    scaled = value / factor
    int_digits = len(str(int(abs(scaled))))
    return f"{scaled:.{max(0, decimals - int_digits + 1)}f}", prefix + unit


def format_duration(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "-"
    if round(seconds, 2) < 60:
        return tr("{value} s", value=f"{seconds:.2f}")
    total = int(round(seconds))          # round first so 3599.6 s is "1 h 0 min"
    if total < 3600:
        m, s = divmod(total, 60)
        return tr("{m} min {s} s", m=m, s=s)
    h, rem = divmod(total, 3600)
    return tr("{h} h {m} min", h=h, m=rem // 60)
