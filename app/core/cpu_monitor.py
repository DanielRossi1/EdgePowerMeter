"""Lightweight CPU usage monitor without external dependencies.

Uses /proc/stat on POSIX systems; falls back to psutil if available elsewhere.
"""

from __future__ import annotations

import os
from typing import Optional


class CPUUsageMonitor:
    """Computes instantaneous CPU usage (percentage).

    Each call compares cumulative CPU times since the previous call.
    """

    def __init__(self) -> None:
        self._prev_total: Optional[int] = None
        self._prev_idle: Optional[int] = None
        self._use_proc = os.name == "posix" and os.path.exists("/proc/stat")
        self._psutil = None
        if not self._use_proc:
            try:
                import psutil  # type: ignore

                self._psutil = psutil
            except Exception:
                self._psutil = None

    def get_usage(self) -> Optional[float]:
        """Return CPU usage percentage or None if unavailable."""
        if self._use_proc:
            return self._get_proc_stat_usage()
        if self._psutil:
            try:
                return float(self._psutil.cpu_percent(interval=None))
            except Exception:
                return None
        return None

    def _get_proc_stat_usage(self) -> Optional[float]:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                line = f.readline()
            parts = line.split()
            if not parts or parts[0] != "cpu":
                return None
            # user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
            values = list(map(int, parts[1:]))
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
        except Exception:
            return None

        if self._prev_total is None or self._prev_idle is None:
            self._prev_total = total
            self._prev_idle = idle
            return None

        delta_total = total - self._prev_total
        delta_idle = idle - self._prev_idle
        self._prev_total = total
        self._prev_idle = idle

        if delta_total <= 0:
            return None

        usage = (delta_total - delta_idle) * 100.0 / delta_total
        # Clamp to [0,100]
        if usage < 0.0:
            usage = 0.0
        elif usage > 100.0:
            usage = 100.0
        return usage
