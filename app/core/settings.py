"""Application settings with persistence (QSettings)."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Dict

from PySide6.QtCore import QSettings

_ORG, _APP = "EdgePowerMeter", "EdgePowerMeter"
_GROUP = "settings_v2"

# Allowed values for enumerated settings (first entry is the fallback).
CHOICES: Dict[str, tuple] = {
    "theme": ("dark", "light", "system"),
    "unit_mode": ("auto", "base", "milli"),
    "avg_power_mode": ("window", "session"),
    "csv_separator": (",", ";", "\t"),
    "csv_decimal": (".", ","),
    "csv_time_format": ("datetime", "epoch"),
    "spectrum_signal": ("current", "power", "voltage"),
}


@dataclass
class AppSettings:
    """All user-configurable settings.

    Field defaults are the factory defaults; `load()` overlays the persisted
    values with type checking so a corrupted entry falls back to the default
    instead of crashing the app.
    """

    # General
    language: str = "auto"
    theme: str = "dark"
    confirm_discard: bool = True

    # Display
    plot_fps: int = 30
    time_window_s: float = 10.0
    line_width: float = 1.5
    antialias: bool = True
    show_grid: bool = True
    grid_alpha: float = 0.2
    show_crosshair: bool = True
    show_voltage: bool = True
    show_current: bool = True
    show_power: bool = True
    unit_mode: str = "auto"
    value_decimals: int = 4
    avg_power_mode: str = "window"
    avg_window_s: float = 1.0
    show_cpu_usage: bool = False
    use_opengl: bool = True

    # Acquisition
    baud_rate: int = 2000000
    auto_reconnect: bool = True
    no_data_timeout_s: float = 15.0
    host_averaging: int = 1
    sync_clock_on_connect: bool = True

    # Calibration (applied on the host)
    shunt_ohm: float = 0.010
    voltage_gain: float = 1.0
    voltage_offset_v: float = 0.0
    current_gain: float = 1.0
    current_offset_a: float = 0.0

    # Recording
    autosave: bool = True
    recordings_dir: str = ""      # empty = Documents/EdgePowerMeter

    # Export
    csv_separator: str = ","
    csv_decimal: str = "."
    csv_time_format: str = "datetime"
    pdf_include_graphs: bool = True
    pdf_include_psu: bool = True
    pdf_include_spectrum: bool = False
    spectrum_signal: str = "current"
    report_title: str = ""
    report_notes: str = ""
    last_dir: str = ""

    def validate(self) -> None:
        """Clamp numeric values and reset invalid enumerations."""
        for name, allowed in CHOICES.items():
            if getattr(self, name) not in allowed:
                setattr(self, name, allowed[0])
        self.plot_fps = min(max(self.plot_fps, 5), 120)
        self.time_window_s = min(max(self.time_window_s, 0.5), 3600.0)
        self.line_width = min(max(self.line_width, 0.5), 5.0)
        self.grid_alpha = min(max(self.grid_alpha, 0.0), 1.0)
        self.value_decimals = min(max(self.value_decimals, 1), 6)
        self.avg_window_s = min(max(self.avg_window_s, 0.05), 600.0)
        self.no_data_timeout_s = min(max(self.no_data_timeout_s, 3.0), 600.0)
        self.host_averaging = min(max(self.host_averaging, 1), 1000)
        if not (1e-5 <= self.shunt_ohm <= 100.0):
            self.shunt_ohm = 0.010
        if not (0.5 <= self.voltage_gain <= 2.0):
            self.voltage_gain = 1.0
        if not (0.5 <= self.current_gain <= 2.0):
            self.current_gain = 1.0

    def copy(self) -> "AppSettings":
        return AppSettings(**{f.name: getattr(self, f.name) for f in fields(self)})

    def as_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def save(self) -> None:
        try:
            s = QSettings(_ORG, _APP)
            s.beginGroup(_GROUP)
            for name, value in self.as_dict().items():
                s.setValue(name, value)
            s.endGroup()
            s.sync()
        except Exception:
            pass

    @classmethod
    def load(cls) -> "AppSettings":
        inst = cls()
        try:
            s = QSettings(_ORG, _APP)
            s.beginGroup(_GROUP)
            stored = {k: s.value(k) for k in s.childKeys()}
            s.endGroup()
            if not stored:
                stored = _migrate_v1(s)
            for f in fields(inst):
                if f.name in stored and stored[f.name] is not None:
                    try:
                        setattr(inst, f.name, _coerce(stored[f.name], getattr(inst, f.name)))
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
        inst.validate()
        return inst


def _coerce(value: Any, default: Any) -> Any:
    # QSettings returns strings for everything on some platforms (INI backend).
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        return bool(value)
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    return str(value)


def _migrate_v1(s: QSettings) -> Dict[str, Any]:
    """Carry over the few settings from app <= 1.7 that still exist."""
    out: Dict[str, Any] = {}
    if s.contains("dark_mode"):
        out["theme"] = "dark" if _coerce(s.value("dark_mode"), True) else "light"
    for key in ("show_grid", "grid_alpha", "show_crosshair", "baud_rate",
                "auto_reconnect", "show_cpu_usage", "csv_separator"):
        if s.contains(key):
            out[key] = s.value(key)
    return out
